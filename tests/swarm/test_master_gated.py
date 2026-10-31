import os
from pathlib import Path

import pytest

from hooks.context import context_recycle
from scripts.swarm import master_gated
from scripts.swarm.store import MASTER, AgentRecord, RedisStore
from tests.swarm.test_tick import FakeLedger, FakeRuntime
from tests.swarm.test_tick import store as store

pytestmark = pytest.mark.unit
MINUTE = 60_000
START = 1_800_000_000_000


@pytest.fixture(autouse=True)
def recycling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(context_recycle, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(context_recycle, "COMPACT_LIMIT", 600)
    monkeypatch.setattr(context_recycle, "HANDOFF_MARGIN", 50)
    monkeypatch.setattr(context_recycle, "codex_context", lambda session_id: None)
    monkeypatch.setattr(context_recycle, "used_tokens", lambda session_id: 700_000)
    return tmp_path


def master(store: RedisStore, home: Path, gated_at: int | None = START) -> tuple[AgentRecord, FakeRuntime]:
    name = store.next_name("sw", MASTER)
    agent = AgentRecord(
        name, MASTER, MASTER, pane_id="w1:p1", started_at=START, seat="master@sw", conversation_id="conv-1"
    )
    store.put_agent("sw", agent)
    store.seats.occupy(agent.seat, agent.name, START)
    if gated_at is not None:
        marker = home / "context_usage" / "conv-1.recycle"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        os.utime(marker, (gated_at / 1000, gated_at / 1000))
    runtime = FakeRuntime()
    runtime.live.add(name)
    return agent, runtime


def run(store: RedisStore, runtime: FakeRuntime, at: int) -> list[str]:
    return master_gated.run("sw", store.config("sw"), store, FakeLedger([]), runtime, at)


def test_a_master_held_at_the_hard_gate_past_the_bound_is_recycled_in_its_seat(
    store: RedisStore, recycling: Path
) -> None:
    agent, runtime = master(store, recycling)
    assert run(store, runtime, START + 14 * MINUTE) == []
    assert store.agents("sw")[0].state != "finished"
    recycled = run(store, runtime, START + 15 * MINUTE)
    assert recycled == [f"recycled master {agent.name} in its seat after 15 minutes held at the context hard gate"]
    assert [a.state for a in store.agents("sw")] == ["finished"]
    handoff = store.handoff("sw", MASTER)
    assert f"{agent.name} sat 15 minutes at the context hard gate" in handoff
    assert handoff.endswith("<!-- handoff complete -->\n")
    assert store.handoff_seat("sw", MASTER) == agent.seat


def test_a_master_never_gated_is_left_alone(store: RedisStore, recycling: Path) -> None:
    _, runtime = master(store, recycling, gated_at=None)
    assert run(store, runtime, START + 60 * MINUTE) == []
    assert store.agents("sw")[0].state != "finished"


def test_a_master_back_under_the_gate_is_left_alone(
    store: RedisStore, recycling: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = master(store, recycling)
    monkeypatch.setattr(context_recycle, "used_tokens", lambda session_id: 100_000)
    assert run(store, runtime, START + 60 * MINUTE) == []
    assert store.agents("sw")[0].state != "finished"


def test_a_stopped_swarm_keeps_its_gated_master(store: RedisStore, recycling: Path) -> None:
    _, runtime = master(store, recycling)
    store.update("sw", state="stopped")
    assert run(store, runtime, START + 60 * MINUTE) == []
