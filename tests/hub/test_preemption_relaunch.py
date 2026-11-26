import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from scripts import serena_router_daemon
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus
from scripts.hive import execution_events
from scripts.hive.lanes import LaneTasks
from scripts.hive.spoke import Spoke, SpokeConfig
from scripts.hub import admission, claims, slots
from scripts.inbox.store import InboxStore
from scripts.swarm import admission_view, preempt, readiness
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2.runtime.base import Outcome, Status
from tests.helpers.narrow_ledger import DocLedger
from tests.swarm.test_readiness import doc, snapshot, task

SLUG = "sw"


@pytest.fixture
def episode(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    import fakeredis

    monkeypatch.setattr(serena_router_daemon, "ensure", lambda: True)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=1, engine="events"))
    found = doc([task("lower"), task("higher", rank="high"), task("urgent", rank="urgent"), task("other")])
    rows = {row["id"]: row for row in found["tasks"]}
    ledger: Any = DocLedger(found)
    source: Any = SimpleNamespace(
        slugs=lambda: [SLUG],
        snapshot=lambda slug: snapshot(found, claimed=[key for key in rows if store.claimant(slug, key)]),
    )
    now = SimpleNamespace(ms=1)
    admitter = admission.Admission(store, ledger, source, clock=lambda: now.ms)
    admitter.view = admission_view.Narrow(
        lambda: store, ledger, source, claims=lambda store, slug: lambda key: store.claimant(slug, key) is not None
    )
    rig = SimpleNamespace(store=store, found=found, rows=rows, ledger=ledger, admitter=admitter, now=now, names=[])
    _hold(rig, "lower", 2)
    _hold(rig, "higher", 1)
    assert _preempt(rig) == []
    now.ms = 2
    assert len(_preempt(rig)) == 1
    assert len(_asks(rig)) == 1
    yield rig
    store.redis.close()


def _hold(rig: Any, task_id: str, number: int) -> AgentRecord:
    name = rig.store.next_name(SLUG, "eng", rig.now.ms)
    record = AgentRecord(name, "eng", task_id, seat=f"eng-{number}@{SLUG}", state="working", started_at=rig.now.ms)
    previous = rig.store.execution_occupants(SLUG).get(record.seat)
    agent = rig.store.start_execution(SLUG, record, previous.execution_id if previous else "")
    assert rig.store.claim(SLUG, task_id, name, 3_600_000)
    rig.ledger.update_task(SLUG, task_id, {"state": "claimed", "claimed_by": name}, if_state=("open",))
    rig.names.append(name)
    return agent


def _leave(rig: Any, task_id: str) -> str:
    name = rig.store.claimant(SLUG, task_id)
    agent = next(agent for agent in rig.store.agents(SLUG) if agent.name == name)
    assert rig.store.release(SLUG, task_id, name)
    assert rig.store.drop_agent(SLUG, name, at=rig.now.ms)
    rig.ledger.update_task(SLUG, task_id, {"state": "open", "claimed_by": ""}, if_state=("claimed",))
    return agent.seat


def _preempt(rig: Any) -> list[str]:
    holders = rig.store.agents(SLUG)
    full = {"eng": ([rig.rows["urgent"]], holders)} if len(holders) == 2 else {}
    return preempt.step(SLUG, rig.store, rig.rows, rig.found, full, rig.now.ms, probe=lambda lane, row: "")


def _asks(rig: Any) -> list[Any]:
    inbox = InboxStore(rig.store.redis)
    return [item for name in rig.names for item in inbox.open_items(name) if item.ref.startswith(f"{preempt.REF}:")]


def _spawn(rig: Any, task_id: str, status: Status) -> Any:
    bus: Any = open_bus(BusConfig())
    slots.serve(bus, admitter=rig.admitter)
    runtime = Mock()
    placed = SimpleNamespace(pane_id="pane-a", harness="claude", account="slot-a", launched_at=rig.now.ms)
    runtime.spawn.return_value = Outcome("spawn", status, "local", placed, detail="no placeable account")
    reading = {"cpus": 32, "load5": 0, "available_mb": 60_000, "total_mb": 64_000}
    spoke = Spoke(
        SpokeConfig(rig.store.config(SLUG), "eng", "hive-a"),
        bus,
        runtime,
        LaneTasks(bus, "hive-a", rig.store.config),
        lambda: reading,
        view=Mock(return_value=None),
    )
    offered = snapshot(rig.found, admits=lambda row: row["id"] == task_id)
    try:
        readiness.publish(bus, offered, rig.now.ms)
        assert spoke.poll() == ("started" if status is Status.OK else "retry")
        request = runtime.spawn.call_args.args[0]
        rig.names.append(request.name)
        if status is Status.OK:
            identity = execution_events.identity(SLUG, request.name, request.task["execution_generation"])
            saved = bus.for_swarm(SLUG).state(execution_events.STARTS).get(identity)
            claims.Claims(rig.store).handle(Envelope.from_dict(json.loads(saved.value)))
        return request
    finally:
        bus.close()


def _refused(rig: Any) -> None:
    _leave(rig, "lower")
    generation = rig.rows["urgent"].get("claim_generation", 0)
    rig.now.ms = preempt.ANSWER_MS
    _spawn(rig, "urgent", Status.UNAVAILABLE)
    assert rig.rows["urgent"]["state"] == "open"
    assert rig.rows["urgent"]["claimed_by"] == ""
    assert rig.store.claimant(SLUG, "urgent") is None
    assert rig.rows["urgent"]["claim_generation"] == generation + 2
    rig.now.ms += 2


@pytest.mark.feature("hub-preemption-suppresses-relaunch")
def test_a_refused_urgent_spawn_keeps_one_ask_after_its_claim_generation_advances(episode: Any) -> None:
    _refused(episode)
    _hold(episode, "other", 2)
    assert _preempt(episode) == []
    assert len(_asks(episode)) == 1


@pytest.mark.feature("hub-preemption-suppresses-relaunch")
def test_the_relaunched_lower_task_is_not_asked_again_for_the_same_urgent_task(episode: Any) -> None:
    _refused(episode)
    request = _spawn(episode, "lower", Status.OK)
    assert request.task["seat"] == f"eng-2@{SLUG}"
    assert _preempt(episode) == []
    assert len(_asks(episode)) == 1


@pytest.mark.feature("hub-preemption-suppresses-relaunch")
def test_the_urgent_task_starts_on_the_freed_seat_after_one_handoff(episode: Any) -> None:
    _refused(episode)
    _spawn(episode, "lower", Status.OK)
    _preempt(episode)
    seat = _leave(episode, "lower")
    request = _spawn(episode, "urgent", Status.OK)
    agent = next(agent for agent in episode.store.agents(SLUG) if agent.name == request.name)
    assert (agent.task, agent.state, agent.seat) == ("urgent", "working", seat)
    assert len(_asks(episode)) == 1


def _successor(rig: Any, elapsed_ms: int) -> AgentRecord:
    _leave(rig, "lower")
    rig.now.ms = 2 + elapsed_ms
    rig.store = RedisStore(rig.store.redis)
    return _hold(rig, "lower", 2)


def _rotate(rig: Any, holders: list[AgentRecord]) -> list[str]:
    rig.rows["other"]["rank"] = "urgent"
    full = {"eng": ([rig.rows["other"]], holders)}
    assert preempt.step(SLUG, rig.store, rig.rows, rig.found, full, rig.now.ms - 1, probe=lambda lane, row: "") == []
    return preempt.step(SLUG, rig.store, rig.rows, rig.found, full, rig.now.ms, probe=lambda lane, row: "")


@pytest.mark.feature("hub-preemption-victim-rotation")
def test_recent_victim_task_rotates_to_other_holder(episode: Any) -> None:
    successor = _successor(episode, 600_000)
    episode.rows["higher"]["rank"] = "low"
    other = next(agent for agent in episode.store.agents(SLUG) if agent.task == "higher")
    actions = _rotate(episode, [other, successor])
    assert len(actions) == 1
    assert f"asked {other.name} to hand off" in actions[0]
    assert len([item for item in _asks(episode) if item.task == "higher"]) == 1
    assert len(_asks(episode)) == 2


@pytest.mark.feature("hub-preemption-victim-rotation")
@pytest.mark.parametrize(
    ("elapsed_ms", "expected_asks"),
    [(600_000, 0), (1_799_999, 0), (1_800_000, 1), (1_800_001, 1)],
)
def test_recent_victim_task_waits_for_thirty_minutes(episode: Any, elapsed_ms: int, expected_asks: int) -> None:
    successor = _successor(episode, elapsed_ms)
    actions = _rotate(episode, [successor])
    assert len(actions) == expected_asks
    assert len(_asks(episode)) == 1 + expected_asks
    if expected_asks:
        assert f"asked {successor.name} to hand off" in actions[0]
