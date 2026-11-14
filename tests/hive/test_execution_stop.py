"""The hub stops an execution it admitted through the hive spoke that started it."""

import json
from collections.abc import Iterator
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.hive import execution_events, execution_stop, lanes
from scripts.hub import lifecycle
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm_v2.runtime.base import Outcome, Status

SLUG = "rig"
HIVE = "hive-a"


def outcome(status: Status = Status.OK, value: Any = "pane") -> Outcome:
    """A runtime answer with the given status and found value."""
    return Outcome("reap", status, "local", value)


def runtime() -> Mock:
    """A hive runtime whose agent is live and reaps cleanly."""
    found = Mock()
    found.find.return_value = outcome()
    found.reap.return_value = outcome()
    return found


@pytest.fixture
def rig() -> Iterator[SimpleNamespace]:
    """A started execution on hive-a with its claim, agent record, ledger row and hub lifecycle."""
    import fakeredis

    bus: Any = memory.open_bus(BusConfig())
    agent = AgentRecord(
        name="engineer", lane="eng", task="task", execution_id="execution", generation=3, seat="eng-1", account="a"
    )
    row = {"id": "task", "state": "claimed", "claimed_by": agent.name, "claim_generation": 7, "done": False}

    def update(*args: Any, **kwargs: Any) -> dict[str, Any]:
        row.update(args[2])
        return dict(row)

    ledger: Any = SimpleNamespace(task=lambda slug, task_id: dict(row), update_task=update)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.redis.hset(store.key(SLUG, "executions"), agent.execution_id, json.dumps(asdict(agent)))
    store.redis.hset(store.key(SLUG, "agents"), agent.name, json.dumps(asdict(agent)))
    store.redis.set(store.key(SLUG, "claim", agent.task), agent.name)
    started = Envelope(
        id="started:claim",
        type="execution.started",
        swarm=SLUG,
        entity="execution",
        key=agent.execution_id,
        version=agent.generation,
        time=100,
        author=HIVE,
        data={
            "task": agent.task,
            "task_generation": 7,
            "claim_key": "claim",
            "claim_revision": 1,
            "generation": agent.generation,
            "lane": "eng",
            "hive": HIVE,
            "name": agent.name,
            "pane_id": "p1",
        },
    )
    bus.for_swarm(SLUG).state("spoke-work").create(
        "claim", json.dumps({"state": "started", "claim_revision": 1, "event": started.to_dict()}).encode()
    )
    execution_events.started(bus, started)
    react = lifecycle.Lifecycle(bus, store, ledger, release=lambda _store, slug, agent: None)
    yield SimpleNamespace(bus=bus, agent=agent, row=row, store=store, started=started, react=react)
    bus.close()
    store.redis.close()


def exits(bus: Any) -> list[Envelope]:
    """Every execution exited event on the rig swarm."""
    found = bus.for_swarm(SLUG).events.consumer("audit-exits", f"swarm.{SLUG}.execution.exited").fetch(10, 0)
    return [item.event for item in found]


@pytest.mark.feature("events-execution-stop")
def test_a_stop_reaps_the_execution_on_its_hive_and_ends_it_through_the_exited_lifecycle(rig: SimpleNamespace) -> None:
    hive = runtime()

    assert execution_stop.stop(rig.bus, SLUG, rig.agent, "launch check failed") == execution_stop.SENT
    assert execution_stop.Stopper(rig.bus, SLUG, HIVE, hive).poll() == [execution_stop.STOPPED]

    hive.reap.assert_called_once_with(rig.agent.name)
    [exited] = exits(rig.bus)
    assert (exited.id, exited.type, exited.key, exited.version, exited.cause) == (
        f"exited:{rig.started.id}",
        "execution.exited",
        rig.agent.execution_id,
        rig.agent.generation,
        rig.started.id,
    )
    assert exited.data["reason"] == execution_stop.REASON
    rig.react.handle(exited)
    assert (rig.row["state"], rig.row["claimed_by"]) == ("open", "")
    assert rig.store.execution_registry.get(SLUG, rig.agent.execution_id).state == "ended"
    assert json.loads(rig.bus.for_swarm(SLUG).state("spoke-work").get("claim").value)["state"] == "exited"


@pytest.mark.feature("events-execution-stop")
def test_a_redelivered_stop_reaps_nothing_more_and_emits_no_second_exit(rig: SimpleNamespace) -> None:
    hive = runtime()
    stopper = execution_stop.Stopper(rig.bus, SLUG, HIVE, hive)
    execution_stop.stop(rig.bus, SLUG, rig.agent, "first")
    stopper.poll()
    event = execution_stop.request(rig.started, "first")

    assert stopper.handle(event) == execution_stop.ENDED
    assert execution_stop.stop(rig.bus, SLUG, rig.agent, "again") == execution_stop.ENDED
    assert stopper.poll() == []
    assert hive.reap.call_count == 1
    assert len(exits(rig.bus)) == 1


@pytest.mark.feature("events-execution-stop")
def test_a_spoke_that_does_not_own_the_execution_ignores_its_stop(rig: SimpleNamespace) -> None:
    other = runtime()
    execution_stop.stop(rig.bus, SLUG, rig.agent, "launch check failed")
    stranger = execution_stop.Stopper(rig.bus, SLUG, "hive-b", other)

    assert stranger.poll() == []
    assert stranger.handle(execution_stop.request(rig.started, "misrouted")) == execution_stop.FOREIGN
    other.find.assert_not_called()
    other.reap.assert_not_called()
    assert exits(rig.bus) == []


@pytest.mark.feature("events-execution-stop")
@pytest.mark.parametrize("state", ["exiting", "exited"])
def test_the_hub_refuses_to_stop_an_execution_that_already_ended(rig: SimpleNamespace, state: str) -> None:
    claims = rig.bus.for_swarm(SLUG).state("spoke-work")
    entry = claims.get("claim")
    claims.update("claim", json.dumps({"state": state, "claim_revision": 1}).encode(), entry.revision)
    hive = runtime()

    assert execution_stop.stop(rig.bus, SLUG, rig.agent, "late") == execution_stop.ENDED
    assert execution_stop.Stopper(rig.bus, SLUG, HIVE, hive).poll() == []
    hive.reap.assert_not_called()


@pytest.mark.feature("events-execution-stop")
def test_the_hub_answers_unknown_for_an_execution_no_hive_started(rig: SimpleNamespace) -> None:
    stranger = AgentRecord(name="ghost", lane="eng", task="task", execution_id="other", generation=1)

    assert execution_stop.stop(rig.bus, SLUG, stranger, "late") == execution_stop.UNKNOWN


@pytest.mark.feature("events-execution-stop")
def test_a_failed_reap_emits_no_exit_and_is_retried(rig: SimpleNamespace) -> None:
    hive = runtime()
    hive.reap.return_value = outcome(Status.REFUSED)
    stopper = execution_stop.Stopper(rig.bus, SLUG, HIVE, hive)
    execution_stop.stop(rig.bus, SLUG, rig.agent, "launch check failed")

    assert stopper.poll() == [execution_stop.FAILED]
    assert exits(rig.bus) == []
    hive.reap.return_value = outcome()
    assert stopper.poll() == [execution_stop.STOPPED]
    assert len(exits(rig.bus)) == 1


@pytest.mark.feature("events-execution-stop")
def test_an_execution_already_gone_from_its_hive_still_ends_without_a_reap(rig: SimpleNamespace) -> None:
    hive = runtime()
    hive.find.return_value = outcome(value=None)
    execution_stop.stop(rig.bus, SLUG, rig.agent, "launch check failed")

    assert execution_stop.Stopper(rig.bus, SLUG, HIVE, hive).poll() == [execution_stop.STOPPED]
    hive.reap.assert_not_called()
    assert len(exits(rig.bus)) == 1


@pytest.mark.feature("events-execution-stop")
@pytest.mark.parametrize("draining", [True, False])
def test_the_lane_loop_honors_stops_every_pass_draining_or_not(monkeypatch: pytest.MonkeyPatch, draining: bool) -> None:
    polled: list[tuple[Any, ...]] = []
    monkeypatch.setattr(lanes, "draining", lambda store, hive: draining)
    monkeypatch.setattr(lanes, "beat", lambda *args: None)
    monkeypatch.setattr(lanes, "run_once", lambda *args: ["started"])
    monkeypatch.setattr(lanes, "stop_once", lambda *args: polled.append(args) or [])

    lanes.run("store", "bus", "runtime", "hive", ("eng",), lambda delay: None, passes=range(2))

    assert [args[:4] for args in polled] == [("store", "bus", "runtime", "hive")] * 2
