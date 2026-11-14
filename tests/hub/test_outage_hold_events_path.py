"""Outage hold for an engineer the hub started over the bus: its exit neither retires nor reclaims it during an outage."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit

SLUG = "rig"
HIVE = "root"
SEAT = f"eng-1@{SLUG}"
START = 900_000
NOW = 1_000_000


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[SimpleNamespace]:
    """An events swarm on a nats bus config whose one engineer the root hive spoke started and the hub owns."""
    import fakeredis

    from scripts.events import memory
    from scripts.events.config import BusConfig
    from scripts.events.envelope import Envelope
    from scripts.hive import execution_events
    from scripts.hub import lifecycle, reconciler
    from scripts.swarm import commands, outage
    from scripts.swarm.store import EVENTS_ENGINE, AgentRecord, RedisStore, SwarmConfig

    monkeypatch.setattr(commands, "hive_id", lambda: HIVE)
    (tmp_path / "hub").mkdir()
    monkeypatch.setattr(outage, "HOME", tmp_path / "hub")
    clock = Mock(return_value=NOW)
    bus: Any = memory.open_bus(BusConfig())
    monkeypatch.setattr(reconciler.BusConfig, "from_environ", lambda: SimpleNamespace(backend="nats"))
    monkeypatch.setattr(reconciler, "open_bus", lambda _config: SimpleNamespace(for_swarm=bus.for_swarm, close=Mock()))
    redis = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(redis)
    store.create(SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=0, engine=EVENTS_ENGINE))
    record = AgentRecord(
        name=store.next_name(SLUG, "eng", START),
        lane="eng",
        task="task",
        seat=SEAT,
        account="a",
        hive=HIVE,
        runtime_target={"host_id": HIVE},
        started_at=START,
        launched_at=START,
        pane_id="pane",
    )
    agent = store.start_execution(SLUG, record)
    store.seats.occupy(SEAT, agent.name, agent.generation)
    store.redis.set(store.key(SLUG, "claim", agent.task), agent.name)
    row = {"id": "task", "state": "claimed", "claimed_by": agent.name, "claim_generation": 7, "done": False}

    def update(*args: Any, **kwargs: Any) -> None:
        """Apply a ledger task write to the row."""
        row.update(args[2])

    ledger: Any = SimpleNamespace(
        task=lambda *_: dict(row), update_task=update, state=lambda _slug: {"tasks": [dict(row)]}
    )
    returned: list[tuple[Any, ...]] = []
    life = lifecycle.Lifecycle(bus, store, ledger, release=lambda *args: returned.append(args))
    started = Envelope(
        id="started",
        type="execution.started",
        swarm=SLUG,
        entity="execution",
        key=agent.execution_id,
        version=agent.generation,
        time=START,
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
    exited = Envelope(
        id="exited:started",
        type="execution.exited",
        swarm=SLUG,
        entity="execution",
        key=agent.execution_id,
        version=agent.generation,
        time=NOW,
        author=HIVE,
        cause="started",
        data=dict(started.data),
    )
    yield SimpleNamespace(
        bus=bus,
        store=store,
        agent=agent,
        row=row,
        ledger=ledger,
        returned=returned,
        life=life,
        exited=exited,
        clock=clock,
        react=reconciler.Reconciler(bus, store=store, lifecycle=life, clock=clock),
        tick_home=tmp_path / "tick",
    )
    bus.close()
    redis.close()


def _lose_hive(rig: SimpleNamespace) -> None:
    """Let the root hive's presence expire, as a hub reading a stalled bus sees it."""
    rig.bus.state("hive-presence").create(HIVE, json.dumps({"expires_ms": 0}).encode())


@contextmanager
def _on_tick(rig: SimpleNamespace) -> Iterator[None]:
    """Run the block with the tick's own outage home, which the hub never reads."""
    from scripts.swarm import outage

    hub, outage.HOME = outage.HOME, rig.tick_home
    try:
        yield
    finally:
        outage.HOME = hub


def _open_outage(rig: SimpleNamespace, kind: str) -> None:
    """Open a swarm outage of ``kind`` in the tick's home and share it as the outage probes step does."""
    from scripts.swarm import outage

    with _on_tick(rig):
        outage.observe(SLUG, kind, [f"{kind} health probe failed"], NOW - 1_000)
        outage.share(rig.store, SLUG, NOW)


def _close_outages(rig: SimpleNamespace, at_ms: int) -> None:
    """Close every open swarm outage of the tick at ``at_ms`` and share the result."""
    from scripts.swarm import outage

    with _on_tick(rig):
        for record in outage.open_records(SLUG):
            outage.observe(SLUG, record["kind"], [], at_ms)
        outage.share(rig.store, SLUG, NOW)


def _kept(rig: SimpleNamespace) -> None:
    """Assert the engineer was neither retired nor reclaimed and its slot was not returned."""
    assert [agent.name for agent in rig.store.agents(SLUG)] == [rig.agent.name]
    assert (rig.row["state"], rig.row["claimed_by"]) == ("claimed", rig.agent.name)
    assert rig.store.redis.get(rig.store.key(SLUG, "claim", "task")) == rig.agent.name
    assert rig.returned == []


def _runtime(rig: SimpleNamespace, nudged: list[str]) -> Any:
    """A runtime whose pane of the engineer is live, idle and showing a provider error."""
    from scripts.swarm import idle

    observed = SimpleNamespace(state=idle.IDLE, provider_error="overloaded", typed="", prompt_title="")

    def nudge(_agent: Any, text: str) -> str:
        """Record the prompt typed into the pane."""
        nudged.append(text)
        return ""

    return SimpleNamespace(live_names=lambda: {rig.agent.name}, observe=lambda _agent: observed, nudge=nudge)


@pytest.mark.feature("events-outage-hold-hub-agents")
def test_the_engineer_is_hub_owned_on_a_nats_bus(rig: SimpleNamespace) -> None:
    from scripts.hub.reconciler import legacy_agents

    assert legacy_agents(rig.store, SLUG) == []


@pytest.mark.feature("events-outage-hold-hub-agents")
@pytest.mark.parametrize("kind", ["redis", "ledger", "provider"])
def test_an_open_outage_holds_a_hub_agent_whose_hive_is_lost(rig: SimpleNamespace, kind: str) -> None:
    from scripts.swarm import outage

    _open_outage(rig, kind)
    assert outage.open_records(SLUG) == []
    _lose_hive(rig)
    rig.react.poll()
    _kept(rig)


@pytest.mark.feature("events-outage-hold-hub-agents")
def test_an_open_outage_holds_the_exit_the_spoke_published(rig: SimpleNamespace) -> None:
    _open_outage(rig, "redis")
    rig.life.handle(rig.exited)
    _kept(rig)
    assert json.loads(rig.bus.for_swarm(SLUG).state("spoke-work").get("claim").value)["state"] == "started"


@pytest.mark.feature("events-outage-hold-hub-agents")
def test_a_pending_outage_resume_holds_the_exit_after_the_outage_closed(rig: SimpleNamespace) -> None:
    from scripts.swarm import resume

    _open_outage(rig, "provider")
    with _on_tick(rig):
        resume.track_outages(SLUG, rig.store, _runtime(rig, []), {"task": dict(rig.row)}, NOW)
    _close_outages(rig, NOW + 1_000)
    assert resume.recovery_pending(rig.store, SLUG, rig.agent)
    rig.life.handle(rig.exited)
    _kept(rig)


@pytest.mark.feature("events-outage-hold-hub-agents")
def test_after_recovery_the_held_hub_agent_gets_the_resume_prompt(rig: SimpleNamespace) -> None:
    from scripts.swarm import resume

    nudged: list[str] = []
    runtime = _runtime(rig, nudged)
    _open_outage(rig, "redis")
    _lose_hive(rig)
    rig.react.poll()
    with _on_tick(rig):
        resume.track_outages(SLUG, rig.store, runtime, {"task": dict(rig.row)}, NOW)
    _close_outages(rig, NOW + 1_000)
    with _on_tick(rig):
        actions = resume.recover_outages(SLUG, rig.store, rig.ledger, runtime, {"task": dict(rig.row)}, NOW + 2_000)
    assert actions == [f"outage resume for {rig.agent.name}"]
    assert len(nudged) == 1
    assert nudged[0].startswith("Service is back after the outage.")
    _kept(rig)


@pytest.mark.feature("events-outage-hold-hub-agents")
def test_once_no_outage_holds_it_the_exit_retires_and_reclaims(rig: SimpleNamespace) -> None:
    _open_outage(rig, "ledger")
    _close_outages(rig, NOW - 500)
    _lose_hive(rig)
    rig.react.poll()
    assert rig.store.agents(SLUG) == []
    assert (rig.row["state"], rig.row["claimed_by"]) == ("open", "")
    assert len(rig.returned) == 1


def _retired(rig: SimpleNamespace) -> None:
    """Assert the exit dropped the engineer and returned its slot."""
    assert rig.store.agents(SLUG) == []
    assert len(rig.returned) == 1


@pytest.mark.feature("events-outage-hold-hub-agents")
def test_an_open_outage_never_pins_an_agent_whose_task_is_done(rig: SimpleNamespace) -> None:
    rig.row.update(done=True)
    _open_outage(rig, "provider")
    rig.life.handle(rig.exited)
    _retired(rig)
    assert rig.row["state"] == "done"


@pytest.mark.feature("events-outage-hold-hub-agents")
def test_an_open_outage_never_pins_a_deliberate_exit(rig: SimpleNamespace) -> None:
    from scripts.swarm import deliberate_exit

    deliberate_exit.record(rig.store, SLUG, rig.agent.name, NOW)
    _open_outage(rig, "redis")
    rig.life.handle(rig.exited)
    _retired(rig)
    assert (rig.row["state"], rig.row["claimed_by"]) == ("open", "")


@pytest.mark.feature("events-outage-hold-hub-agents")
def test_an_open_outage_never_pins_a_handoff_exit(rig: SimpleNamespace) -> None:
    from dataclasses import replace

    rig.store.put_agent(SLUG, replace(rig.agent, state="finished"))
    rig.row.update(state="handoff")
    _open_outage(rig, "provider")
    rig.life.handle(rig.exited)
    _retired(rig)
    assert rig.row["state"] == "handoff"


@pytest.mark.feature("events-outage-hold-hub-agents")
def test_a_stale_share_of_an_open_outage_still_holds_and_is_logged(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import outage

    _open_outage(rig, "redis")
    key = rig.store.key(SLUG, outage.SHARED)
    assert rig.store.redis.ttl(key) == -1
    assert outage.shared_open(rig.store, SLUG, NOW + outage.SHARED_STALE_MS + 1)
    rig.life.handle(rig.exited)
    _kept(rig)
    assert f"outage share of {SLUG} is stale since {NOW}, still holding redis" in capsys.readouterr().err


@pytest.mark.feature("events-outage-hold-hub-agents")
def test_the_outage_probes_step_shares_the_tick_outages(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import outage
    from scripts.tick.steps import host

    monkeypatch.setattr(outage, "providers", lambda *_args: None)
    with _on_tick(rig):
        outage.observe(SLUG, "ledger", ["Ledger health probe failed"], NOW)
        values = {"store": rig.store, "slug": SLUG, "runtime": None}
        host._outage_probes(SimpleNamespace(now_ms=lambda: NOW), values.__getitem__)
    assert [record["kind"] for record in outage.shared_open(rig.store, SLUG)] == ["ledger"]
