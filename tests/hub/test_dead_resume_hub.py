import json
from collections.abc import Iterator
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit

HIVE = "hive"
NOW = 1_000_000


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    import fakeredis

    from hooks.context import context_recycle
    from scripts.events import memory
    from scripts.events.config import BusConfig
    from scripts.events.envelope import Envelope
    from scripts.hive import execution_events, registry, reports
    from scripts.hub import lifecycle, reconciler
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    clock = Mock(return_value=NOW)
    bus = cast(Any, memory.open_bus(BusConfig()))
    monkeypatch.setattr(reconciler.BusConfig, "from_environ", lambda: SimpleNamespace(backend="nats"))
    monkeypatch.setattr(reconciler, "open_bus", lambda _: bus)
    monkeypatch.setattr(bus, "close", lambda: None)
    monkeypatch.setattr(context_recycle, "_used", lambda session: 240_000)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    agent = AgentRecord(
        name="engineer",
        lane="eng",
        task="task",
        execution_id="execution",
        generation=3,
        seat="eng-1@rig",
        state="working",
        account="a",
        harness="claude",
        profile="engineer",
        conversation_id="conversation",
        hive=HIVE,
        runtime_target={"host_id": HIVE},
        started_at=900_000,
        launched_at=900_000,
        pane_id="pane",
    )
    store.create(SwarmConfig("rig", "/repo", max_eng=1, max_ci=0, engine="events"))
    store.redis.hset(store.key("rig", "executions"), agent.execution_id, json.dumps(asdict(agent)))
    store.redis.hset(store.key("rig", "agents"), agent.name, json.dumps(asdict(agent)))
    store.redis.set(store.key("rig", "claim", agent.task), agent.name)
    store.seats.occupy(agent.seat, agent.name, agent.started_at)
    row = {"id": "task", "state": "claimed", "claimed_by": agent.name, "claim_generation": 7, "done": False}
    ledger: Any = SimpleNamespace(task=lambda *_: dict(row), writes=[], notices=[])

    def update(*args: Any, **kwargs: Any) -> None:
        ledger.writes.append((args, kwargs))
        row.update(args[2])

    ledger.update_task = update
    ledger.notify = lambda slug, text: ledger.notices.append(text)
    returned = []
    life = lifecycle.Lifecycle(bus, store, ledger, release=lambda *args: returned.append(args))
    started = Envelope(
        id="started",
        type="execution.started",
        swarm="rig",
        entity="execution",
        key="execution",
        version=3,
        time=900_000,
        author=HIVE,
        data={
            "name": "engineer",
            "task": "task",
            "hive": HIVE,
            "claim_key": "claim",
            "claim_revision": 1,
            "task_generation": 7,
        },
    )
    execution_events.started(bus, started)
    bus.state("spoke-work").create("claim", json.dumps({"state": "started", "claim_revision": 1}).encode())

    def report(pane_state: str = "exited") -> None:
        store.redis.sadd(registry.INDEX, HIVE)
        body = {
            "observed_at": clock(),
            "pane_ids": [],
            "executions": {
                "execution": {"slug": "rig", "name": "engineer", "generation": 3, "pane": {"state": pane_state}}
            },
        }
        store.redis.set(reports.key(HIVE), json.dumps(body))

    report()
    react = reconciler.Reconciler(bus, store=store, lifecycle=life, clock=clock)
    yield SimpleNamespace(
        bus=bus,
        store=store,
        agent=agent,
        row=row,
        ledger=ledger,
        returned=returned,
        clock=clock,
        report=report,
        react=react,
    )
    store.redis.close()


def _commands(rig: SimpleNamespace) -> list[Any]:
    from scripts.hive.orders import CommandOrder, Orders

    entries = rig.store.redis.xrange(Orders(rig.store.redis).commands(HIVE))
    return [CommandOrder(**json.loads(fields["order"])) for _, fields in entries]


def _next_pass(rig: SimpleNamespace, pane_state: str = "exited") -> None:
    rig.clock.return_value += 300_000
    rig.report(pane_state)
    rig.react.poll()


def _hive(rig: SimpleNamespace, outcome: Any) -> tuple[Any, Mock]:
    from scripts.hive.orders import Consumer

    runtime = Mock()
    runtime.recover.return_value = outcome
    runtime.terminate.return_value = SimpleNamespace(ok=True)
    return Consumer(rig.store.redis, HIVE, runtime), runtime


def _resumed(launched_at: int = 1_150_000) -> Any:
    from scripts.swarm.tick import Placed
    from scripts.swarm_v2.runtime.base import Outcome, Status

    return Outcome("recover", Status.OK, "local", Placed("pane-2", "claude", "a", launched_at=launched_at))


@pytest.mark.feature("hub-dead-resume")
def test_a_dead_hub_started_agent_is_resumed_once_through_its_hive(rig: SimpleNamespace) -> None:
    from scripts.hub.reconciler import legacy_agents

    assert legacy_agents(rig.store, "rig") == []
    rig.react.poll()
    [order] = _commands(rig)
    assert (order.action, order.slug, order.name, order.execution_id, order.generation) == (
        "resume",
        "rig",
        "engineer",
        "execution",
        3,
    )
    assert rig.ledger.writes == rig.returned == []
    assert rig.row["claimed_by"] == "engineer"
    assert rig.store.claimant("rig", "task") == "engineer"
    assert [agent.name for agent in rig.store.agents("rig")] == ["engineer"]
    _next_pass(rig)
    assert len(_commands(rig)) == 1
    assert rig.ledger.writes == rig.returned == []


@pytest.mark.feature("hub-dead-resume")
def test_the_hive_reopens_the_conversation_in_the_same_seat(rig: SimpleNamespace) -> None:
    from scripts.hive.orders import DONE, Orders
    from scripts.swarm_v2.runtime.base import Recovery

    rig.react.poll()
    [order] = _commands(rig)
    consumer, runtime = _hive(rig, _resumed())
    assert consumer.poll_commands() == 1
    agent, mode, _config, text = runtime.recover.call_args.args
    assert (agent.name, agent.conversation_id, agent.seat, mode) == (
        "engineer",
        "conversation",
        "eng-1@rig",
        Recovery.RESUME,
    )
    assert "Continue your task in this conversation and seat" in text
    [resumed] = rig.store.agents("rig")
    assert (resumed.pane_id, resumed.launched_at, resumed.seat) == ("pane-2", 1_150_000, "eng-1@rig")
    assert cast(Any, Orders(rig.store.redis).command(HIVE, order)).status == DONE
    _next_pass(rig, "working")
    assert len(_commands(rig)) == 1
    assert rig.ledger.writes == rig.returned == []


@pytest.mark.feature("hub-dead-resume")
def test_a_resumed_pane_that_dies_again_orders_the_next_resume(rig: SimpleNamespace) -> None:
    rig.react.poll()
    consumer, _runtime = _hive(rig, _resumed(launched_at=rig.agent.launched_at))
    consumer.poll_commands()
    _next_pass(rig)
    assert [order.id for order in _commands(rig)] == ["resume:execution:3:1", "resume:execution:3:2"]
    assert rig.ledger.writes == rig.returned == []
    assert [agent.pane_id for agent in rig.store.agents("rig")] == ["pane-2"]


@pytest.mark.feature("hub-dead-resume")
@pytest.mark.parametrize(
    ("successor", "orders", "returned"),
    [("none", 1, 0), ("seat", 0, 1), ("claim", 0, 1), ("row", 0, 1), ("generation", 0, 1)],
)
def test_a_successors_work_is_never_resumed_by_the_old_agent(
    rig: SimpleNamespace, successor: str, orders: int, returned: int
) -> None:
    if successor == "seat":
        rig.store.seats.occupy(rig.agent.seat, "successor", 950_000)
    elif successor == "claim":
        rig.store.redis.set(rig.store.key("rig", "claim", "task"), "successor")
    elif successor == "row":
        rig.row["claimed_by"] = "successor"
    elif successor == "generation":
        rig.row["claim_generation"] = 8
    rig.react.poll()
    assert len(_commands(rig)) == orders
    assert len(rig.returned) == returned


@pytest.mark.feature("hub-dead-resume")
def test_the_hive_refuses_a_resume_once_a_successor_holds_the_claim(rig: SimpleNamespace) -> None:
    from scripts.hive.orders import FOREIGN, Orders

    rig.react.poll()
    [order] = _commands(rig)
    rig.store.redis.set(rig.store.key("rig", "claim", "task"), "successor")
    consumer, runtime = _hive(rig, _resumed())
    consumer.poll_commands()
    runtime.recover.assert_not_called()
    assert Orders(rig.store.redis).command(HIVE, order) == FOREIGN
    [agent] = rig.store.agents("rig")
    assert agent.pane_id == "pane"


@pytest.mark.feature("hub-dead-resume")
def test_a_failed_resume_hands_the_task_to_a_fresh_agent(rig: SimpleNamespace) -> None:
    from scripts.swarm_v2.runtime.base import Outcome, Status

    rig.react.poll()
    consumer, _runtime = _hive(rig, Outcome("recover", Status.REFUSED, "local", detail="no conversation"))
    consumer.poll_commands()
    _next_pass(rig)
    assert len(_commands(rig)) == 1
    assert rig.ledger.notices == ["Starting a fresh agent after engineer ended because resume failed: no conversation."]
    assert rig.row["state"] == "open"
    assert rig.row["claimed_by"] == ""
    assert len(rig.returned) == 1


@pytest.mark.feature("hub-dead-resume")
def test_the_resume_attempt_cap_hands_the_task_to_a_fresh_agent(rig: SimpleNamespace) -> None:
    from scripts.swarm.resume import RECOVERY_ATTEMPTS

    rig.react.poll()
    for attempt in range(RECOVERY_ATTEMPTS):
        assert len(_commands(rig)) == attempt + 1
        assert rig.ledger.writes == rig.returned == []
        consumer, _runtime = _hive(rig, _resumed(launched_at=rig.clock() + 1))
        consumer.poll_commands()
        _next_pass(rig)
    assert len(_commands(rig)) == RECOVERY_ATTEMPTS
    assert rig.row["state"] == "open"
    assert len(rig.returned) == 1
