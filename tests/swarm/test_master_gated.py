import os
from pathlib import Path

import pytest

from hooks.context import context_recycle
from scripts.handoff import transfers
from scripts.swarm import master_gated
from scripts.swarm.store import MASTER, AgentRecord, RedisStore
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime
from tests.swarm.test_tick import store as store

pytestmark = pytest.mark.unit
MINUTE = 60_000
START = 1_800_000_000_000


def use(monkeypatch: pytest.MonkeyPatch, tokens: int) -> None:
    monkeypatch.setattr(context_recycle, "used_tokens", lambda session_id: tokens if session_id == "conv-1" else None)


@pytest.fixture(autouse=True)
def recycling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(context_recycle, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(context_recycle, "COMPACT_LIMIT", 600)
    monkeypatch.setattr(context_recycle, "HANDOFF_MARGIN", 50)
    monkeypatch.setattr(context_recycle, "codex_context", lambda session_id: None)
    use(monkeypatch, 700_000)
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


def run(store: RedisStore, at: int) -> list[str]:
    return master_gated.run("sw", store.config("sw"), store, FakeLedger([]), at)


def recycled_line(agent: AgentRecord, minutes: int) -> str:
    return f"recycled master {agent.name} in its seat after {minutes} minutes held at the context hard gate"


def test_a_master_held_at_the_hard_gate_past_the_bound_is_recycled_in_its_seat(
    store: RedisStore, recycling: Path
) -> None:
    agent, _ = master(store, recycling)
    assert run(store, START + 14 * MINUTE) == []
    assert store.agents("sw")[0].state != "finished"
    assert run(store, START + 15 * MINUTE) == [recycled_line(agent, 15)]
    assert [a.state for a in store.agents("sw")] == ["finished"]
    handoff = store.handoff("sw", MASTER)
    assert f"Continue as the master of sw; {agent.name} sat 15 minutes at the context hard gate" in handoff
    assert handoff.endswith("<!-- handoff complete -->\n")
    assert store.handoff_seat("sw", MASTER) == agent.seat
    assert [row["reason"] for row in transfers.list_transfers(store, "sw")] == [master_gated.GATED]


@pytest.mark.parametrize("tokens, gated", [(650_000, True), (649_999, False)])
def test_the_bound_counts_from_exactly_the_hard_gate(
    store: RedisStore, recycling: Path, monkeypatch: pytest.MonkeyPatch, tokens: int, gated: bool
) -> None:
    agent, _ = master(store, recycling)
    use(monkeypatch, tokens)
    assert run(store, START + 15 * MINUTE) == ([recycled_line(agent, 15)] if gated else [])


def test_the_swarm_compact_limit_sets_the_hard_gate(
    store: RedisStore, recycling: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = master(store, recycling, gated_at=None)
    store.update("sw", compact_limit=100)
    monkeypatch.setattr(context_recycle, "COMPACT_LIMIT", 100)
    use(monkeypatch, 150_000)
    assert "CONTEXT RECYCLE" in str(
        context_recycle.directive("conv-1", {"AGENTIHOOKS_AGENT_NAME": agent.name, "AGENTIHOOKS_SWARM": "sw"})
    )
    marker = recycling / "context_usage" / "conv-1.recycle"
    os.utime(marker, (START / 1000, START / 1000))
    monkeypatch.setattr(context_recycle, "COMPACT_LIMIT", 600)
    assert run(store, START + 15 * MINUTE) == [recycled_line(agent, 15)]


def test_a_master_never_gated_is_left_alone(store: RedisStore, recycling: Path) -> None:
    master(store, recycling, gated_at=None)
    assert run(store, START + 60 * MINUTE) == []
    assert store.agents("sw")[0].state != "finished"


def test_a_master_back_under_the_gate_is_left_alone(
    store: RedisStore, recycling: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    master(store, recycling)
    use(monkeypatch, 100_000)
    assert run(store, START + 60 * MINUTE) == []
    assert store.agents("sw")[0].state != "finished"


def test_a_swarm_without_a_master_has_nothing_to_recycle(store: RedisStore) -> None:
    assert run(store, START + 60 * MINUTE) == []


@pytest.mark.parametrize("state", ["stopping", "stopped"])
def test_a_stopping_swarm_keeps_its_gated_master(store: RedisStore, recycling: Path, state: str) -> None:
    master(store, recycling)
    store.update("sw", state=state)
    assert run(store, START + 60 * MINUTE) == []
    assert store.agents("sw")[0].state != "finished"


def test_the_tick_recycles_a_master_held_at_the_hard_gate(store: RedisStore, recycling: Path) -> None:
    agent, runtime = master(store, recycling)
    actions = tick("sw", store, FakeLedger([]), runtime, START + 15 * MINUTE)
    assert recycled_line(agent, 15) in actions
    assert [row["reason"] for row in transfers.list_transfers(store, "sw")] == [master_gated.GATED]
