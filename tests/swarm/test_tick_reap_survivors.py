"""Mutation-killing tests for scripts.swarm.tick: _reap, _reap_agent and tick."""

import pytest

from scripts.swarm import tick as tick_module
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import STARTUP_GRACE_MS


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    result = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    result.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, compact_limit=600))
    return result


class _StubRuntime:
    def live_names(self) -> set[str]:
        return set()


def test_reap_groups_agents_by_task_falling_back_to_name(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.hub import reconciler

    agents = [
        AgentRecord(name="agent-a", lane="eng", task="t1", state="awaiting-decision"),
        AgentRecord(name="agent-b", lane="eng", task="t1", state="awaiting-decision"),
        AgentRecord(name="agent-c", lane="eng", task="", state="awaiting-decision"),
    ]
    monkeypatch.setattr(reconciler, "legacy_agents", lambda store_arg, slug_arg: agents)
    captured: list[list[list[int]]] = []

    def spying_fan_out(action: object, items: object) -> list[object]:
        values = [list(item) for item in items]
        captured.append(values)
        return [action(value) for value in values]

    monkeypatch.setattr(tick_module.pool, "fan_out", spying_fan_out)

    actions = tick_module._reap("sw", store, None, _StubRuntime(), {}, 0)

    assert actions == []
    assert captured == [[[0, 1], [2]]]


def test_reap_agent_forwards_its_own_ledger_to_watch_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def spying_watch_remote(
        slug: str, store: object, ledger: object, runtime: object, rows: object, agent: object, now_ms: int
    ) -> list[str]:
        seen["ledger"] = ledger
        return []

    monkeypatch.setattr(tick_module, "_watch_remote", spying_watch_remote)

    ledger = object()
    agent = AgentRecord(name="agent-a", lane="eng", task="t1", state="working", runtime_backend="remote")
    judging = tick_module._Reaping("sw", object(), ledger, _StubRuntime(), {}, set(), 5)

    actions = tick_module._reap_agent(judging, agent)

    assert actions == []
    assert seen["ledger"] is ledger


def test_reap_agent_forwards_its_own_now_ms_to_lost(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def spying_lost(
        slug: str, store: object, ledger: object, runtime: object, rows: object, agent: object, now_ms: int
    ) -> list[str]:
        seen["now_ms"] = now_ms
        return []

    monkeypatch.setattr(tick_module, "_lost", spying_lost)

    agent = AgentRecord(name="agent-a", lane="eng", task="t1", state="working", started_at=0)
    now_ms = STARTUP_GRACE_MS + 5
    judging = tick_module._Reaping("sw", store, object(), _StubRuntime(), {}, set(), now_ms)

    actions = tick_module._reap_agent(judging, agent)

    assert actions == []
    assert seen["now_ms"] == now_ms


def test_tick_hands_the_close_group_its_freshly_spawned_rows(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.tick.steps import tasks as task_steps_module
    from tests.swarm.test_tick import FakeRuntime, tasks

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    seen: dict[str, str] = {}
    original_run_group = tick_module.run_group

    def spying_run_group(name: str, **state: object) -> list[str]:
        if name == task_steps_module.CLOSE:
            seen["claimed_by"] = state["rows"]["t1"]["claimed_by"]
        return original_run_group(name, **state)

    monkeypatch.setattr(tick_module, "run_group", spying_run_group)

    tick_module.tick("sw", store, ledger, runtime, 1)

    spawned = next(agent.name for agent in store.agents("sw") if agent.lane == "eng")
    assert seen["claimed_by"] == spawned
