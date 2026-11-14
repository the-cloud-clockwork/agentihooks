"""The hub stops an execution it admitted through the hive spoke that started it."""

import json
from collections.abc import Iterator
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig, QueuePolicy
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
    """A started execution on hive-a with its claim, agent record, ledger row and hub lifecycle, on a bus whose clock
    the test moves."""
    import fakeredis

    now = [0]
    bus: Any = memory.open_bus(BusConfig(), clock=lambda: now[0])
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
    yield SimpleNamespace(bus=bus, agent=agent, row=row, store=store, started=started, react=react, now=now)
    bus.close()
    store.redis.close()


def exits(bus: Any) -> list[Envelope]:
    """Every execution exited event on the rig swarm."""
    found = bus.for_swarm(SLUG).events.consumer("audit-exits", f"swarm.{SLUG}.execution.exited").fetch(10, 0)
    return [item.event for item in found]


def end_claim(rig: SimpleNamespace, state: str) -> None:
    """Moves the rig's claim out of the started state, as the hub lifecycle does when the execution exits."""
    claims = rig.bus.for_swarm(SLUG).state("spoke-work")
    entry = claims.get("claim")
    claims.update("claim", json.dumps({"state": state, "claim_revision": 1}).encode(), entry.revision)


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
    assert (exited.data["reason"], exited.data["stop_reason"]) == (execution_stop.REASON, "launch check failed")
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
    hive.find.return_value = outcome(value=None)

    assert stopper.handle(execution_stop.request(rig.started, "first")) == execution_stop.ENDED
    assert execution_stop.stop(rig.bus, SLUG, rig.agent, "again") == execution_stop.ENDED
    assert stopper.poll() == []
    assert hive.reap.call_count == 1
    assert len(exits(rig.bus)) == 1


@pytest.mark.feature("events-execution-stop")
def test_a_second_stop_inside_the_dedupe_window_answers_pending(rig: SimpleNamespace) -> None:
    assert execution_stop.stop(rig.bus, SLUG, rig.agent, "first") == execution_stop.SENT
    assert execution_stop.stop(rig.bus, SLUG, rig.agent, "again") == execution_stop.PENDING


@pytest.mark.feature("events-execution-stop")
def test_a_spoke_that_does_not_own_the_execution_ignores_its_stop(rig: SimpleNamespace) -> None:
    other = runtime()
    execution_stop.stop(rig.bus, SLUG, rig.agent, "launch check failed")
    stranger = execution_stop.Stopper(rig.bus, SLUG, "hive-b", other)
    stop = execution_stop.request(rig.started, "misrouted")

    assert stranger.poll() == []
    assert stranger.handle(stop) == execution_stop.FOREIGN
    assert stranger.handle(replace(stop, data={**stop.data, "hive": "hive-b"})) == execution_stop.FOREIGN
    assert execution_stop.Stopper(rig.bus, SLUG, HIVE, other).handle(replace(stop, key="other")) == (
        execution_stop.FOREIGN
    )
    other.find.assert_not_called()
    other.reap.assert_not_called()
    assert exits(rig.bus) == []


@pytest.mark.feature("events-execution-stop")
@pytest.mark.parametrize("state", ["exiting", "exited"])
def test_the_hub_refuses_to_stop_an_execution_that_already_ended(rig: SimpleNamespace, state: str) -> None:
    end_claim(rig, state)
    hive = runtime()

    assert execution_stop.stop(rig.bus, SLUG, rig.agent, "late") == execution_stop.ENDED
    assert execution_stop.Stopper(rig.bus, SLUG, HIVE, hive).poll() == []
    hive.reap.assert_not_called()


@pytest.mark.feature("events-execution-stop")
def test_the_hub_refuses_to_stop_an_execution_whose_claim_is_gone(rig: SimpleNamespace) -> None:
    agent = replace(rig.agent, name="orphan", execution_id="orphaned")
    data = {**rig.started.data, "name": agent.name, "claim_key": "gone"}
    execution_events.started(rig.bus, replace(rig.started, id="started:gone", key=agent.execution_id, data=data))

    assert execution_stop.stop(rig.bus, SLUG, agent, "late") == execution_stop.ENDED


@pytest.mark.feature("events-execution-stop")
def test_the_hub_answers_unknown_for_an_execution_no_hive_started(rig: SimpleNamespace) -> None:
    stranger = AgentRecord(name="ghost", lane="eng", task="task", execution_id="other", generation=1)

    assert execution_stop.stop(rig.bus, SLUG, stranger, "late") == execution_stop.UNKNOWN


@pytest.mark.feature("events-execution-stop")
def test_a_stop_reaching_a_hive_after_its_execution_ended_still_reaps_a_surviving_agent(rig: SimpleNamespace) -> None:
    hive = runtime()
    execution_stop.stop(rig.bus, SLUG, rig.agent, "launch check failed")
    end_claim(rig, "exited")

    assert execution_stop.Stopper(rig.bus, SLUG, HIVE, hive).poll() == [execution_stop.ENDED]
    hive.reap.assert_called_once_with(rig.agent.name)
    assert exits(rig.bus) == []


@pytest.mark.feature("events-execution-stop")
def test_a_failed_reap_emits_no_exit_and_is_retried_past_the_default_delivery_limit(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    hive = runtime()
    hive.reap.return_value = outcome(Status.REFUSED)
    stopper = execution_stop.Stopper(rig.bus, SLUG, HIVE, hive)
    execution_stop.stop(rig.bus, SLUG, rig.agent, "launch check failed")

    for _ in range(QueuePolicy().max_deliver + 1):
        assert stopper.poll() == [execution_stop.FAILED]
        assert stopper.poll() == []
        rig.now[0] += execution_stop.RETRY_MS
    assert exits(rig.bus) == []
    assert f"stop for {rig.agent.name} in swarm {SLUG} could not reap it" in capsys.readouterr().err
    hive.reap.return_value = outcome()
    assert stopper.poll() == [execution_stop.STOPPED]
    assert len(exits(rig.bus)) == 1


@pytest.mark.feature("events-execution-stop")
def test_a_stop_whose_handling_raises_is_retried_and_the_rest_of_the_batch_settles(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    hive = runtime()
    hive.find.side_effect = [RuntimeError("herdr down"), outcome()]
    stopper = execution_stop.Stopper(rig.bus, SLUG, HIVE, hive)
    execution_stop.stop(rig.bus, SLUG, rig.agent, "launch check failed")

    assert stopper.poll() == [execution_stop.FAILED]
    assert "failed (RuntimeError: herdr down)" in capsys.readouterr().err
    rig.now[0] += execution_stop.RETRY_MS
    assert stopper.poll() == [execution_stop.STOPPED]


@pytest.mark.feature("events-execution-stop")
def test_an_execution_already_gone_from_its_hive_still_ends_without_a_reap(rig: SimpleNamespace) -> None:
    hive = runtime()
    hive.find.return_value = outcome(value=None)
    execution_stop.stop(rig.bus, SLUG, rig.agent, "launch check failed")

    assert execution_stop.Stopper(rig.bus, SLUG, HIVE, hive).poll() == [execution_stop.STOPPED]
    hive.reap.assert_not_called()
    assert len(exits(rig.bus)) == 1


@pytest.mark.feature("events-execution-stop")
def test_a_hive_reads_nothing_and_says_nothing_before_the_hub_creates_its_stop_consumer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from nats.js.errors import NotFoundError

    bus = Mock()
    bus.for_swarm.return_value = bus
    bus.events.consumer.side_effect = NotFoundError()
    stopper = execution_stop.Stopper(bus, SLUG, HIVE, runtime())

    assert stopper.poll() == []
    assert capsys.readouterr().err == ""
    bus.events.consumer.side_effect = RuntimeError("refused")
    assert stopper.poll() == []
    assert "failed (RuntimeError: refused)" in capsys.readouterr().err


@pytest.mark.feature("events-execution-stop")
def test_the_lane_loop_stops_executions_in_every_events_swarm_and_keeps_each_stopper(rig: SimpleNamespace) -> None:
    from tests.hive.test_lanes import configs

    hive = runtime()
    held: dict[str, execution_stop.Stopper] = {}
    store = configs({SLUG: "events", "old": "legacy"})
    execution_stop.stop(rig.bus, SLUG, rig.agent, "launch check failed")

    assert lanes.stop_once(store, rig.bus, hive, HIVE, held) == [execution_stop.STOPPED]
    kept = held[SLUG]
    assert lanes.stop_once(store, rig.bus, hive, HIVE, held) == []
    assert held == {SLUG: kept}
    hive.reap.assert_called_once_with(rig.agent.name)


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
