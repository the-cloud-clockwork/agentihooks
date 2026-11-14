import json
from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit

HIVE = "hive"
NOW = 1_000_000
LAUNCHED = 900_000


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
    store.create(SwarmConfig("rig", "/repo", max_eng=1, max_ci=0, engine="events", repo_origin="/hive/repo"))
    name = store.next_name("rig", "eng")
    assert store.claim("rig", "task", name, 600_000)
    admitted = store.start_execution(
        "rig", AgentRecord(name, "eng", "task", seat="eng-1@rig", state="starting", started_at=LAUNCHED, hive=HIVE)
    )
    store.seats.occupy(admitted.seat, name, LAUNCHED)
    agent = replace(
        admitted,
        state="working",
        harness="claude",
        profile="engineer",
        account="a",
        pane_id="pane",
        launched_at=LAUNCHED,
        conversation_id="conversation",
        idle_ticks=5,
    )
    store.put_agent("rig", agent)
    row = {"id": "task", "state": "claimed", "claimed_by": name, "claim_generation": 7, "done": False}
    ledger: Any = SimpleNamespace(
        task=lambda slug, task_id: dict(row) if (slug, task_id) == ("rig", "task") else None, writes=[], notices=[]
    )

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
        key=agent.execution_id,
        version=agent.generation,
        time=LAUNCHED,
        author=HIVE,
        data={
            "name": name,
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
        record = {"slug": "rig", "name": name, "generation": agent.generation, "pane": {"state": pane_state}}
        body = {"observed_at": clock(), "pane_ids": [], "executions": {agent.execution_id: record}}
        store.redis.set(reports.key(HIVE), json.dumps(body))

    report()
    react = reconciler.Reconciler(bus, store=store, lifecycle=life, clock=clock)
    yield SimpleNamespace(
        bus=bus,
        store=store,
        agent=agent,
        started=started,
        row=row,
        ledger=ledger,
        returned=returned,
        clock=clock,
        report=report,
        life=life,
        react=react,
    )
    store.redis.close()


def _commands(rig: SimpleNamespace) -> list[Any]:
    from scripts.hive.orders import CommandOrder, Orders

    entries = rig.store.redis.xrange(Orders(rig.store.redis).commands(HIVE))
    return [CommandOrder(**json.loads(fields["order"])) for _, fields in entries]


def _sent(rig: SimpleNamespace) -> list[str]:
    from scripts.hive.orders import Orders

    prefix = Orders(rig.store.redis).command_receipt(HIVE, "")
    keys = rig.store.redis.scan_iter(f"{prefix}resume:*")
    return sorted(key.removeprefix(prefix) for key in keys if not key.endswith(":running"))


def _next_pass(rig: SimpleNamespace, pane_state: str = "exited") -> None:
    rig.clock.return_value += 300_000
    rig.report(pane_state)
    rig.react.poll()


def _hive(rig: SimpleNamespace, outcome: Any) -> tuple[Any, Mock]:
    from scripts.hive.orders import Consumer

    runtime = Mock()
    runtime.recover.return_value = outcome
    runtime.terminate.return_value = SimpleNamespace(ok=True)
    runtime.reap.return_value = SimpleNamespace(ok=True)
    consumer = Consumer(rig.store.redis, HIVE, runtime)
    consumer.offbeat = consumer.run_resume
    return consumer, runtime


def _resumed(launched_at: int = 1_150_000) -> Any:
    from scripts.swarm.tick import Placed
    from scripts.swarm_v2.runtime.base import Outcome, Status

    placed = Placed("pane-2", "claude", "a", launched_at=launched_at)
    return Outcome("recover", Status.OK, "local", placed)


@pytest.mark.feature("hub-dead-resume")
def test_a_dead_hub_started_agent_is_resumed_once_through_its_hive(rig: SimpleNamespace) -> None:
    from scripts.hub.reconciler import legacy_agents

    assert legacy_agents(rig.store, "rig") == []
    rig.react.poll()
    [order] = _commands(rig)
    assert (order.action, order.slug, order.name, order.execution_id, order.generation) == (
        "resume",
        "rig",
        rig.agent.name,
        rig.agent.execution_id,
        rig.agent.generation,
    )
    assert (order.launched_at, order.pane_id) == (LAUNCHED, "pane")
    assert rig.ledger.writes == rig.returned == []
    assert rig.row["claimed_by"] == rig.agent.name
    assert rig.store.claimant("rig", "task") == rig.agent.name
    assert [agent.name for agent in rig.store.agents("rig")] == [rig.agent.name]
    _next_pass(rig)
    assert len(_sent(rig)) == 1
    assert rig.ledger.writes == rig.returned == []


@pytest.mark.feature("hub-dead-resume")
def test_the_hive_reopens_the_conversation_in_the_same_seat(rig: SimpleNamespace) -> None:
    from scripts.hive.orders import DONE, Orders
    from scripts.swarm_v2.runtime.base import Recovery

    rig.react.poll()
    [order] = _commands(rig)
    consumer, runtime = _hive(rig, _resumed())
    assert consumer.poll_commands() == 1
    agent, mode, config, text = runtime.recover.call_args.args
    assert (agent.name, agent.conversation_id, agent.seat, mode) == (
        rig.agent.name,
        "conversation",
        "eng-1@rig",
        Recovery.RESUME,
    )
    assert config.repo == "/hive/repo"
    assert "Continue your task in this conversation and seat" in text
    [resumed] = rig.store.agents("rig")
    assert (resumed.pane_id, resumed.launched_at, resumed.seat, resumed.idle_ticks) == (
        "pane-2",
        1_150_000,
        "eng-1@rig",
        0,
    )
    assert cast(Any, Orders(rig.store.redis).command(HIVE, order)).status == DONE
    _next_pass(rig, "working")
    assert len(_sent(rig)) == 1
    assert rig.ledger.writes == rig.returned == []


@pytest.mark.feature("hub-dead-resume")
def test_the_hive_runs_a_resume_order_off_its_heartbeat_and_keeps_it_until_answered(rig: SimpleNamespace) -> None:
    from scripts.hive.orders import DONE, Orders

    rig.react.poll()
    [order] = _commands(rig)
    consumer, runtime = _hive(rig, _resumed())
    consumer.offbeat = Mock()
    receipt = Orders(rig.store.redis).command_receipt(HIVE, order.id)
    assert consumer.poll_commands() == 1
    consumer.offbeat.assert_called_once_with(order)
    runtime.recover.assert_not_called()
    assert Orders(rig.store.redis).command(HIVE, order) is None
    assert consumer.poll_commands() == 1
    assert consumer.offbeat.call_count == 1
    rig.store.redis.delete(f"{receipt}:running")
    consumer.poll_commands()
    assert consumer.offbeat.call_count == 2
    consumer.run_resume(order)
    consumer.poll_commands()
    assert _commands(rig) == []
    assert cast(Any, Orders(rig.store.redis).command(HIVE, order)).status == DONE


@pytest.mark.feature("hub-dead-resume")
def test_an_order_without_a_judged_launch_parses_on_an_older_hive() -> None:
    from scripts.hive.orders import CommandOrder

    older = {"id", "action", "slug", "name", "execution_id", "generation", "text"}
    assert set(json.loads(CommandOrder("retire:x:1", "retire", "rig", "n", "x", 1).encode())) == older
    resume = json.loads(CommandOrder("resume:x:1:1", "resume", "rig", "n", "x", 1, "go", 5, "p").encode())
    assert (resume["launched_at"], resume["pane_id"]) == (5, "p")


@pytest.mark.feature("hub-dead-resume")
def test_a_resumed_pane_that_dies_again_orders_the_next_resume(rig: SimpleNamespace) -> None:
    rig.react.poll()
    consumer, _runtime = _hive(rig, _resumed(launched_at=LAUNCHED))
    consumer.poll_commands()
    _next_pass(rig)
    prefix = f"resume:{rig.agent.execution_id}:{rig.agent.generation}"
    assert _sent(rig) == [f"{prefix}:1", f"{prefix}:2"]
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
    assert len(_sent(rig)) == orders
    assert len(rig.returned) == returned


@pytest.mark.feature("hub-dead-resume")
@pytest.mark.parametrize("change", ["claim", "relaunch"])
def test_the_hive_refuses_a_resume_the_hub_judged_on_another_record(rig: SimpleNamespace, change: str) -> None:
    from scripts.hive.orders import FOREIGN, Orders

    rig.react.poll()
    [order] = _commands(rig)
    if change == "claim":
        rig.store.redis.set(rig.store.key("rig", "claim", "task"), "successor")
    else:
        rig.store.put_agent("rig", replace(rig.agent, pane_id="pane-3", launched_at=LAUNCHED + 1))
    consumer, runtime = _hive(rig, _resumed())
    consumer.poll_commands()
    runtime.recover.assert_not_called()
    assert Orders(rig.store.redis).command(HIVE, order) == FOREIGN


@pytest.mark.feature("hub-dead-resume")
def test_a_deliberately_ended_hub_agent_is_not_resumed(rig: SimpleNamespace) -> None:
    from scripts.swarm import deliberate_exit

    deliberate_exit.record(rig.store, "rig", rig.agent.name, NOW)
    rig.react.poll()
    assert _sent(rig) == []
    assert rig.row["state"] == "open"
    assert len(rig.returned) == 1
    assert not deliberate_exit.recorded(rig.store, "rig", rig.agent.name)


@pytest.mark.feature("hub-dead-resume")
@pytest.mark.parametrize("reason", ["lost", "hive-lost", "stopped"])
def test_an_agent_on_a_lost_hive_is_handed_to_a_fresh_agent(rig: SimpleNamespace, reason: str) -> None:
    exited = replace(
        rig.started,
        id=f"{reason}:started",
        type="execution.exited",
        time=NOW,
        data={**rig.started.data, "reason": reason},
    )
    rig.life.handle(exited)
    assert _sent(rig) == []
    assert rig.row["state"] == "open"
    assert len(rig.returned) == 1


@pytest.mark.feature("hub-dead-resume")
def test_a_failed_resume_hands_the_task_to_a_fresh_agent(rig: SimpleNamespace) -> None:
    from scripts.swarm_v2.runtime.base import Outcome, Status

    rig.react.poll()
    consumer, _runtime = _hive(rig, Outcome("recover", Status.REFUSED, "local", detail="no conversation"))
    consumer.poll_commands()
    _next_pass(rig)
    assert len(_sent(rig)) == 1
    assert rig.ledger.notices == [
        f"Starting a fresh agent after {rig.agent.name} ended because resume failed: no conversation."
    ]
    assert rig.row["state"] == "open"
    assert rig.row["claimed_by"] == ""
    assert len(rig.returned) == 1


@pytest.mark.feature("hub-dead-resume")
def test_an_unanswered_resume_hands_the_task_to_a_fresh_agent_at_its_deadline(rig: SimpleNamespace) -> None:
    from scripts.hive.orders import RESUME_DEADLINE_MS

    rig.react.poll()
    _next_pass(rig)
    assert rig.ledger.writes == rig.returned == []
    rig.clock.return_value += RESUME_DEADLINE_MS
    _next_pass(rig)
    assert len(_sent(rig)) == 1
    assert rig.ledger.notices == [
        f"Starting a fresh agent after {rig.agent.name} ended because resume failed: its hive never answered."
    ]
    assert rig.row["state"] == "open"
    assert len(rig.returned) == 1


@pytest.mark.feature("hub-dead-resume")
def test_the_hive_reaps_a_live_process_of_the_name_before_it_resumes(rig: SimpleNamespace) -> None:
    from scripts.swarm_v2.runtime.base import Outcome, Status

    rig.react.poll()
    consumer, runtime = _hive(rig, _resumed())
    runtime.reap.return_value = Outcome("reap", Status.REFUSED, "local", detail="a process carrying it still runs")
    consumer.poll_commands()
    runtime.reap.assert_called_once_with(rig.agent.name)
    runtime.recover.assert_not_called()
    _next_pass(rig)
    assert rig.row["state"] == "open"
    assert rig.ledger.notices == [
        f"Starting a fresh agent after {rig.agent.name} ended because resume failed: a process carrying it still runs."
    ]


@pytest.mark.feature("hub-dead-resume")
def test_the_hive_ends_the_new_pane_when_a_successor_claims_during_the_resume(rig: SimpleNamespace) -> None:
    from scripts.hive.orders import FOREIGN, Orders

    rig.react.poll()
    [order] = _commands(rig)
    consumer, runtime = _hive(rig, _resumed())

    def claimed(*_args: Any) -> Any:
        rig.store.redis.set(rig.store.key("rig", "claim", "task"), "successor")
        return _resumed()

    runtime.recover.side_effect = claimed
    consumer.poll_commands()
    ended = runtime.terminate.call_args.args[0]
    assert (ended.name, ended.pane_id) == (rig.agent.name, "pane-2")
    assert Orders(rig.store.redis).command(HIVE, order) == FOREIGN


@pytest.mark.feature("hub-dead-resume")
def test_an_exit_read_before_the_resumed_launch_is_held(rig: SimpleNamespace) -> None:
    rig.react.poll()
    consumer, _runtime = _hive(rig, _resumed())
    consumer.poll_commands()
    stale = replace(
        rig.started, id="exited:started", type="execution.exited", time=LAUNCHED + 1, data=dict(rig.started.data)
    )
    rig.life.handle(stale)
    assert len(_sent(rig)) == 1
    assert rig.ledger.writes == rig.returned == []


@pytest.mark.feature("hub-dead-resume")
def test_a_stopped_swarm_resumes_no_agent(rig: SimpleNamespace) -> None:
    rig.store.update("rig", state="stopped")
    rig.react.poll()
    assert _sent(rig) == []
    assert len(rig.returned) == 1


@pytest.mark.feature("hub-dead-resume")
def test_a_row_without_a_claim_generation_matches_the_first_generation(rig: SimpleNamespace) -> None:
    rig.row.pop("claim_generation")
    exited = replace(
        rig.started,
        id="exited:started",
        type="execution.exited",
        time=NOW,
        data={**rig.started.data, "task_generation": 0},
    )
    rig.life.handle(exited)
    assert len(_sent(rig)) == 1


@pytest.mark.feature("hub-dead-resume")
def test_the_legacy_gate_admits_a_dead_agent_that_still_holds_its_seat_and_claim(rig: SimpleNamespace) -> None:
    from scripts.swarm import dead_resume

    assert dead_resume.resumable(rig.store, "rig", rig.agent, rig.ledger) is True


@pytest.mark.feature("hub-dead-resume")
def test_the_resume_attempt_cap_hands_the_task_to_a_fresh_agent(rig: SimpleNamespace) -> None:
    from scripts.swarm.resume import RECOVERY_ATTEMPTS

    rig.react.poll()
    for attempt in range(RECOVERY_ATTEMPTS):
        assert len(_sent(rig)) == attempt + 1
        assert rig.ledger.writes == rig.returned == []
        consumer, _runtime = _hive(rig, _resumed(launched_at=rig.clock() + 1))
        consumer.poll_commands()
        _next_pass(rig)
    assert len(_sent(rig)) == RECOVERY_ATTEMPTS
    assert rig.row["state"] == "open"
    assert len(rig.returned) == 1
