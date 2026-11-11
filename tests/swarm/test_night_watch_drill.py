"""The night watch drill command drives an enforcing night watch to a recorded recovery on a scratch swarm."""

import json
from dataclasses import replace
from typing import Any

import pytest

from scripts.gates import log as gate_log
from scripts.swarm import capacity, cli, night_watch_drill, tick
from scripts.swarm.health import merge_stall, night_watch
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import MANUAL_SCALING, MASTER, AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2 import front_door

pytestmark = pytest.mark.unit

SCRATCH = "proof-abc123-eng-1"
NOW = 1_800_000_000_000
QUIET = {"repo": "o/r", "queue": 0, "merged": 0, "failed": "none observed", "streak": 0, "head": 0}


class Ledger:
    """A ledger client that records alerts and serves an empty document."""

    def __init__(self) -> None:
        """Start with no alerts."""
        self.alerts: list[tuple[str, str, str]] = []
        self.read: list[str] = []

    def tasks(self, slug: str) -> list[dict[str, Any]]:
        """Record the swarm whose tasks were read and serve one task."""
        self.read.append(slug)
        return [{"id": "t1"}]

    def raise_alert(self, slug: str, source: str, text: str) -> None:
        """Record a raised alert."""
        self.alerts.append((slug, source, text))


class Runtime:
    """A runtime whose live agent names the test sets."""

    def __init__(self) -> None:
        """Start with no live agents."""
        self.live: set[str] = set()

    def live_names(self) -> set[str]:
        """Return the live agent names."""
        return set(self.live)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> dict[str, Any]:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    for slug, scratch in ((SCRATCH, True), ("live-swarm", False)):
        store.create(
            SwarmConfig(slug, "/repo", max_eng=1, max_ci=0, max_plan=0, scaling=MANUAL_SCALING, scratch=scratch)
        )
        store.update(slug, state="running")
    ledger, runtime, started, docs, routes = Ledger(), Runtime(), [], [], []

    def ready_work(slug: str, store: RedisStore, doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[Any]]]:
        docs.append(doc)
        return {}, {"eng": [], "ci": [], "plan": []}

    def routed(**kwargs: Any) -> Runtime:
        routes.append(kwargs)
        return runtime

    def master(slug: str, config: SwarmConfig, held: RedisStore, live: Runtime, at: int) -> list[str]:
        started.append((slug, at))
        return [f"started master of {slug}"]

    monkeypatch.setattr(gate_log, "swarm_home", lambda: tmp_path)
    monkeypatch.setattr(merge_stall, "observe", lambda repo, at: {**QUIET, "at": at})
    monkeypatch.setattr(capacity, "ready_work", ready_work)
    monkeypatch.setattr(tick, "_master", master)
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient", lambda: ledger)
    monkeypatch.setattr("scripts.swarm_v2.runtime.routed.routed", routed)
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)
    return {"store": store, "ledger": ledger, "runtime": runtime, "started": started, "docs": docs, "routes": routes}


def no_pass(*args: Any) -> list[str]:
    """A night watch pass that takes no action."""
    return []


def drill(store: RedisStore, slug: str) -> None:
    args = cli.build_parser().parse_args([slug, "night-watch-drill"])
    args.handler(store, args)


@pytest.mark.feature("swarm-night-watch-drill")
def test_the_drill_respawns_the_empty_front_door_seat_of_a_scratch_swarm_and_records_it(
    rig: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    store = rig["store"]
    drill(store, SCRATCH)
    printed = json.loads(capsys.readouterr().out)
    stored = json.loads(store.redis.get(store.key(SCRATCH, night_watch_drill.KEY)))
    assert printed == stored
    assert stored["swarm"] == SCRATCH and stored["at"] == NOW
    assert rig["ledger"].read == [SCRATCH]
    assert rig["docs"] and all(doc == {"tasks": [{"id": "t1"}]} for doc in rig["docs"])
    assert [list(kwargs) for kwargs in rig["routes"]] == [["herdr"]]
    assert isinstance(rig["routes"][0]["herdr"], HerdrRuntime)
    assert stored["recovered"] is True and stored["respawned"] == NOW and stored["mode"] == "enforce"
    assert f"started master of {SCRATCH}" in stored["actions"]
    assert rig["started"] == [(SCRATCH, NOW)]
    assert store.config(SCRATCH).gates["nightwatch"] == "enforce"
    assert [source for _, source, _ in rig["ledger"].alerts] == ["seat"]
    assert night_watch.read(store, SCRATCH)["respawns"] == [NOW]
    assert stored["observed_since"] is None


@pytest.mark.feature("swarm-night-watch-drill")
def test_the_drill_records_when_the_night_watch_first_saw_the_empty_seat(
    rig: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    store = rig["store"]
    seen = NOW - 30 * 60_000
    store.redis.set(store.key(SCRATCH, night_watch.KEY), json.dumps({"at": NOW - 60_000, "seat": {"since": seen}}))
    drill(store, SCRATCH)
    stored = json.loads(capsys.readouterr().out)
    assert stored["observed_since"] == seen and stored["recovered"] is True
    assert night_watch.read(store, SCRATCH)["seat"]["since"] == seen
    assert rig["started"] == [(SCRATCH, NOW)]


@pytest.mark.feature("swarm-night-watch-drill")
def test_the_drill_refuses_a_live_swarm_and_leaves_it_untouched(rig: dict[str, Any]) -> None:
    store = rig["store"]
    with pytest.raises(SwarmError, match=f"^{night_watch_drill.NOT_SCRATCH.format(slug='live-swarm')}$"):
        drill(store, "live-swarm")
    assert store.config("live-swarm").gates == {}
    assert night_watch.read(store, "live-swarm") == {}
    assert rig["started"] == []


@pytest.mark.feature("swarm-night-watch-drill")
def test_the_drill_refuses_a_scratch_swarm_that_is_not_running(rig: dict[str, Any]) -> None:
    store = rig["store"]
    store.update(SCRATCH, state="paused")
    with pytest.raises(SwarmError, match=f"^{night_watch_drill.NOT_RUNNING.format(slug=SCRATCH, state='paused')}$"):
        drill(store, SCRATCH)
    assert store.config(SCRATCH).gates == {} and rig["started"] == []


@pytest.mark.feature("swarm-night-watch-drill")
def test_the_drill_refuses_a_scratch_swarm_whose_master_seat_a_live_master_holds(rig: dict[str, Any]) -> None:
    store = rig["store"]
    seat = front_door.seat(SCRATCH)
    store.put_agent(SCRATCH, AgentRecord("master@proof", MASTER, MASTER, started_at=NOW, state="working", seat=seat))
    rig["runtime"].live.add("master@proof")
    with pytest.raises(SwarmError, match=f"^{night_watch_drill.SEAT_HELD.format(slug=SCRATCH)}$"):
        drill(store, SCRATCH)
    assert store.config(SCRATCH).gates == {} and rig["started"] == []
    assert store.redis.get(store.key(SCRATCH, night_watch_drill.KEY)) is None


@pytest.mark.feature("swarm-night-watch-drill")
@pytest.mark.parametrize("last", [8, 30])
def test_the_drill_drops_a_seat_stall_from_stale_night_watch_passes_and_recovers_once(
    rig: dict[str, Any], capsys: pytest.CaptureFixture[str], last: int
) -> None:
    store = rig["store"]
    state = {"at": NOW - last * 60_000, "seat": {"since": NOW - 60 * 60_000}}
    store.redis.set(store.key(SCRATCH, night_watch.KEY), json.dumps(state))
    drill(store, SCRATCH)
    stored = json.loads(capsys.readouterr().out)
    assert stored["observed_since"] is None and stored["recovered"] is True
    assert rig["started"] == [(SCRATCH, NOW)]
    assert night_watch.read(store, SCRATCH)["seat"]["since"] == NOW - 10 * 60_000


@pytest.mark.feature("swarm-night-watch-drill")
def test_the_drill_ages_a_new_stall_when_stale_passes_saw_no_empty_seat(
    rig: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    store = rig["store"]
    store.redis.set(store.key(SCRATCH, night_watch.KEY), json.dumps({"at": NOW - 30 * 60_000}))
    drill(store, SCRATCH)
    stored = json.loads(capsys.readouterr().out)
    assert stored["observed_since"] is None and stored["recovered"] is True
    assert rig["started"] == [(SCRATCH, NOW)]


@pytest.mark.feature("swarm-night-watch-drill")
def test_the_drill_keeps_a_stall_seen_by_passes_exactly_at_the_gap(
    rig: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    store = rig["store"]
    seen = NOW - 30 * 60_000
    state = {"at": NOW - night_watch.GAP_MS, "seat": {"since": seen}}
    store.redis.set(store.key(SCRATCH, night_watch.KEY), json.dumps(state))
    drill(store, SCRATCH)
    assert json.loads(capsys.readouterr().out)["observed_since"] == seen


@pytest.mark.feature("swarm-night-watch-drill")
def test_a_fresh_stall_younger_than_the_threshold_records_no_recovery(
    rig: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    store = rig["store"]
    seen = NOW - 3 * 60_000
    store.redis.set(store.key(SCRATCH, night_watch.KEY), json.dumps({"at": NOW - 60_000, "seat": {"since": seen}}))
    drill(store, SCRATCH)
    stored = json.loads(capsys.readouterr().out)
    assert stored["observed_since"] == seen and stored["respawned"] is None
    assert stored["recovered"] is False and rig["started"] == []


@pytest.mark.feature("swarm-night-watch-drill")
def test_the_drill_reads_the_ledger_document_to_see_a_starting_master_hold_the_seat(rig: dict[str, Any]) -> None:
    store = rig["store"]
    store.put_agent(SCRATCH, AgentRecord("master@proof", MASTER, MASTER, started_at=NOW - 1, state="starting"))
    rig["runtime"].live.add("master@proof")
    doc = {"tasks": [], "_meta": {"events": [{"by": "master@proof", "at": NOW}]}}
    found = night_watch_drill.Drill(store, SCRATCH, rig["ledger"], rig["runtime"], doc, NOW, no_pass)
    assert night_watch_drill.refusal(found) == night_watch_drill.SEAT_HELD.format(slug=SCRATCH)
    assert night_watch_drill.refusal(replace(found, doc={"tasks": []})) == ""


@pytest.mark.feature("swarm-night-watch-drill")
def test_a_master_retired_with_no_new_tasks_leaves_the_drill_without_a_recovery(
    rig: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    store = rig["store"]
    store.redis.set(store.key(SCRATCH, "master-retired-tasks"), json.dumps(["t1"]))
    drill(store, SCRATCH)
    stored = json.loads(capsys.readouterr().out)
    assert stored["recovered"] is False and stored["respawned"] is None and rig["started"] == []
    assert night_watch.read(store, SCRATCH).get("seat") is None
