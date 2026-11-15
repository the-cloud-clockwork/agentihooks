"""Pane evidence, hub reconciler and exits sweep on an events engine swarm whose agent the events path seated."""

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit

SLUG = "rig"
HIVE = "root"
SEAT = f"eng-1@{SLUG}"


def _herdr_dead(args: list[str]) -> dict[str, Any]:
    """A herdr that no longer knows the pane."""
    raise RuntimeError(" ".join(args))


def _herdr_live(name: str) -> Any:
    """A herdr whose pane belongs to ``name`` and shows a working screen."""

    def call(args: list[str]) -> dict[str, Any]:
        """Answer one herdr call for the live pane."""
        if args[:2] == ["agent", "get"]:
            return {"agent": {"name": name, "pane_id": "pane", "agent_status": "working"}}
        if args[:2] == ["pane", "read"]:
            return {"text": "working on the task"}
        return {"process_info": {"shell_pid": 7, "foreground_processes": []}}

    return call


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[SimpleNamespace]:
    """An events swarm with one engineer started through next_name and start_execution on the root hive."""
    import fakeredis

    from scripts.events import memory
    from scripts.events.config import BusConfig
    from scripts.events.envelope import Envelope
    from scripts.hive import execution_events, registry, reports
    from scripts.hub import lifecycle, reconciler
    from scripts.inbox.store import InboxStore
    from scripts.swarm import commands
    from scripts.swarm.store import EVENTS_ENGINE, AgentRecord, RedisStore, SwarmConfig

    monkeypatch.setattr(commands, "hive_id", lambda: HIVE)
    clock = Mock(return_value=1_000_000)
    bus: Any = memory.open_bus(BusConfig())
    redis = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(redis)
    inbox = InboxStore(redis)
    store.create(SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=0, engine=EVENTS_ENGINE))
    record = AgentRecord(
        name=store.next_name(SLUG, "eng", 900_000),
        lane="eng",
        task="task",
        seat=SEAT,
        account="a",
        hive=HIVE,
        runtime_target={"host_id": HIVE},
        started_at=900_000,
        launched_at=900_000,
        pane_id="pane",
    )
    agent = store.start_execution(SLUG, record)
    store.seats.occupy(SEAT, agent.name, agent.generation)
    store.redis.set(store.key(SLUG, "claim", agent.task), agent.name)
    row = {"id": "task", "state": "claimed", "claimed_by": agent.name, "claim_generation": 7, "done": False}
    ledger: Any = SimpleNamespace(task=lambda *_: dict(row))

    def update(*args: Any, **kwargs: Any) -> dict[str, Any]:
        """Apply a ledger task write to the row."""
        row.update(args[2])
        return dict(row)

    ledger.update_task = update
    returned: list[tuple[Any, ...]] = []
    life = lifecycle.Lifecycle(bus, store, ledger, release=lambda *args: returned.append(args))
    started = Envelope(
        id="started",
        type="execution.started",
        swarm=SLUG,
        entity="execution",
        key=agent.execution_id,
        version=agent.generation,
        time=900_000,
        author=HIVE,
        data={
            "name": agent.name,
            "task": agent.task,
            "hive": HIVE,
            "claim_key": "claim",
            "claim_revision": 1,
            "task_generation": 7,
        },
    )
    execution_events.started(bus, started)
    bus.for_swarm(SLUG).state("spoke-work").create(
        "claim", json.dumps({"state": "started", "claim_revision": 1}).encode()
    )
    handled: list[str] = []
    handle = life.handle

    def observed(event: Envelope) -> None:
        """Record the type of each event the reconciler hands the lifecycle, then handle it."""
        handled.append(event.type)
        handle(event)

    life.handle = observed

    def report(state: str) -> None:
        """Save a fresh root hive report showing the agent's pane in ``state``."""
        body = {
            "observed_at": clock(),
            "pane_ids": ["pane"] if state not in execution_events.ENDED else [],
            "executions": {
                agent.execution_id: {
                    "slug": SLUG,
                    "name": agent.name,
                    "generation": agent.generation,
                    "pane": {"state": state},
                }
            },
        }
        store.redis.sadd(registry.INDEX, HIVE)
        store.redis.set(reports.key(HIVE), json.dumps(body))

    item = inbox.send("sender", agent.name, "contract")
    yield SimpleNamespace(
        store=store,
        inbox=inbox,
        agent=agent,
        row=row,
        report=report,
        item=item,
        returned=returned,
        handled=handled,
        react=reconciler.Reconciler(bus, store=store, lifecycle=life, clock=clock),
        home=tmp_path,
    )
    bus.close()
    redis.close()


def _sweeps(rig: SimpleNamespace, herdr: Any) -> list[str]:
    """Run the pane evidence sweep, the hub reconciler and the exits sweep in tick order."""
    from scripts.inbox import exits
    from scripts.swarm import pane_evidence

    runtime = SimpleNamespace(home=rig.home, herdr=herdr)
    actions = pane_evidence.sweep(SLUG, rig.store, runtime, 1_000_000)
    rig.react.poll()
    exits.sweep(rig.inbox, SLUG, rig.store, lambda: {"task": dict(rig.row)})
    return actions


def _incidents(rig: SimpleNamespace) -> list[dict[str, Any]]:
    """The pane evidence incident records written for the swarm."""
    return [json.loads(path.read_text()) for path in (rig.home / SLUG / "pane-evidence").glob("*.incident.json")]


@pytest.mark.feature("events-sweeps-dead-pane")
def test_a_dead_pane_is_found_and_its_execution_exited(rig: SimpleNamespace) -> None:
    from scripts.inbox.store import CLOSED

    rig.report("dead")
    actions = _sweeps(rig, _herdr_dead)
    assert actions == [f"pane evidence saved for {rig.agent.name}: pane lookup failed: RuntimeError"]
    assert [row["agent"] for row in _incidents(rig)] == [rig.agent.name]
    assert rig.store.agents(SLUG) == []
    assert (rig.row["state"], rig.row["claimed_by"]) == ("open", "")
    assert rig.handled == ["execution.exited"]
    assert len(rig.returned) == 1
    settled = rig.inbox.get(rig.item.id)
    assert settled.address == SEAT
    assert settled.state not in CLOSED
    assert rig.store.seats.exits([rig.agent.name])[rig.agent.name]


@pytest.mark.feature("events-sweeps-live-agent")
def test_a_live_agent_is_never_exited(rig: SimpleNamespace) -> None:
    rig.report("running")
    actions = _sweeps(rig, _herdr_live(rig.agent.name))
    assert actions == []
    assert _incidents(rig) == []
    assert [agent.name for agent in rig.store.agents(SLUG)] == [rig.agent.name]
    assert (rig.row["state"], rig.row["claimed_by"]) == ("claimed", rig.agent.name)
    assert rig.handled == []
    assert rig.returned == []
    kept = rig.inbox.get(rig.item.id)
    assert (kept.address, kept.state) == (rig.agent.name, rig.item.state)
    assert not rig.store.seats.exits([rig.agent.name])[rig.agent.name]


@pytest.mark.feature("events-sweeps-live-agent")
def test_the_observe_group_keeps_both_sweeps() -> None:
    from scripts.tick.steps import observe

    names = [step.name for step in observe.STEPS if step.group == "observe"]
    assert {"pane evidence", "exits sweep"} <= set(names)
