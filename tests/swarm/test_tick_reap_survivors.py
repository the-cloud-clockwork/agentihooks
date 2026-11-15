"""Mutation-killing tests for scripts.swarm.tick: _reap, _reap_agent and tick."""

from collections.abc import Callable, Iterable
from typing import Any

import pytest

from scripts.swarm import tick as tick_module
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import STARTUP_GRACE_MS
from scripts.tick.steps import agents as agent_steps


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

    def spying_fan_out(action: Callable[[list[int]], object], items: Iterable[Iterable[int]]) -> list[object]:
        values = [list(item) for item in items]
        captured.append(values)
        return [action(value) for value in values]

    monkeypatch.setattr(tick_module.pool, "fan_out", spying_fan_out)

    actions = agent_steps._reap("sw", store, None, _StubRuntime(), {}, 0)

    assert actions == []
    assert captured == [[[0, 1], [2]]]


def test_reap_keeps_taskless_agents_in_groups_of_their_own_names(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hub import reconciler

    agents = [
        AgentRecord(name="agent-a", lane="eng", task="", state="awaiting-decision"),
        AgentRecord(name="agent-b", lane="eng", task="", state="awaiting-decision"),
        AgentRecord(name="agent-c", lane="eng", task="t1", state="awaiting-decision"),
        AgentRecord(name="agent-d", lane="eng", task="t1", state="awaiting-decision"),
    ]
    monkeypatch.setattr(reconciler, "legacy_agents", lambda store_arg, slug_arg: agents)
    groups: list[list[str]] = []

    def spying_fan_out(action: Callable[[list[int]], object], items: Iterable[Iterable[int]]) -> list[object]:
        values = [list(item) for item in items]
        groups.extend([agents[index].name for index in value] for value in values)
        return [action(value) for value in values]

    monkeypatch.setattr(tick_module.pool, "fan_out", spying_fan_out)

    assert agent_steps._reap("sw", store, None, _StubRuntime(), {}, 0) == []
    assert groups == [["agent-a"], ["agent-b"], ["agent-c", "agent-d"]]


def test_reap_agent_forwards_its_own_ledger_to_watch_remote(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def spying_watch_remote(
        slug: str, store: object, ledger: object, runtime: object, rows: object, agent: object, now_ms: int
    ) -> list[str]:
        seen["ledger"] = ledger
        return []

    monkeypatch.setattr(tick_module, "_watch_remote", spying_watch_remote)

    ledger = object()
    agent = AgentRecord(name="agent-a", lane="eng", task="t1", state="working", runtime_backend="remote")
    judging = tick_module._Reaping("sw", store, ledger, _StubRuntime(), {}, set(), 5)

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


class _LiveRuntime:
    def __init__(self, names: set[str]) -> None:
        self.names = names
        self.named: list[str] = []

    def live_names(self) -> set[str]:
        return self.names

    def name_pane(self, agent: AgentRecord) -> None:
        self.named.append(agent.name)


def _local_judging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tick_module, "_acknowledging", lambda slug, store, agent: False)
    monkeypatch.setattr(tick_module, "_remote_gone_reason", lambda slug, store, runtime, agent, now_ms: None)
    monkeypatch.setattr(tick_module.reports, "host", lambda agent: "")
    monkeypatch.setattr(tick_module, "_elsewhere", lambda agent: False)


def test_reap_agent_does_not_count_an_agent_lost_at_exactly_the_startup_grace(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _local_judging(monkeypatch)
    called: list[int] = []
    monkeypatch.setattr(
        tick_module,
        "_lost",
        lambda slug, store, ledger, runtime, rows, agent, now_ms: called.append(now_ms) or ["lost"],
    )
    agent = AgentRecord(name="agent-a", lane="eng", task="t1", state="working", started_at=1000)
    judging = tick_module._Reaping("sw", store, object(), _StubRuntime(), {}, set(), 1000 + STARTUP_GRACE_MS)

    assert tick_module._reap_agent(judging, agent) == []
    assert called == []


def test_reap_agent_forwards_its_own_ledger_to_watch_agent_for_a_live_master(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _local_judging(monkeypatch)
    seen: list[tuple[object, ...]] = []

    def spying_watch_agent(
        slug: str, store: object, ledger: object, runtime: object, rows: object, agent: object, now_ms: int
    ) -> list[str]:
        seen.append((slug, store, ledger, runtime, rows, agent, now_ms))
        return ["watched"]

    monkeypatch.setattr(tick_module, "_watch_agent", spying_watch_agent)
    ledger, rows = object(), {}
    runtime = _LiveRuntime({"master-a"})
    agent = AgentRecord(name="master-a", lane=tick_module.MASTER, task="", state="working")
    judging = tick_module._Reaping("sw", store, ledger, runtime, rows, {"master-a"}, 7)

    assert tick_module._reap_agent(judging, agent) == ["watched"]
    assert seen == [("sw", store, ledger, runtime, rows, agent, 7)]
    assert runtime.named == ["master-a"]


def test_reap_agent_retires_a_live_agent_whose_task_ended_after_its_owner_was_restored(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _local_judging(monkeypatch)
    answers = iter([False, True])
    monkeypatch.setattr(tick_module, "_ended", lambda agent, rows: next(answers))
    monkeypatch.setattr(tick_module, "_restore_owner", lambda slug, store, ledger, rows, agent: None)
    seen: list[tuple[object, ...]] = []

    def spying_retire_ended(
        slug: str, store: object, ledger: object, runtime: object, rows: object, agent: object, now_ms: int
    ) -> list[str]:
        seen.append((slug, store, ledger, runtime, rows, agent, now_ms))
        return ["retired"]

    monkeypatch.setattr(tick_module, "_retire_ended", spying_retire_ended)
    ledger, rows = object(), {}
    runtime = _LiveRuntime({"agent-a"})
    agent = AgentRecord(name="agent-a", lane="eng", task="t1", state="working")
    judging = tick_module._Reaping("sw", store, ledger, runtime, rows, {"agent-a"}, 9)

    assert tick_module._reap_agent(judging, agent) == ["retired"]
    assert seen == [("sw", store, ledger, runtime, rows, agent, 9)]


def test_reap_agent_forwards_every_argument_to_watch_remote(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tick_module, "_acknowledging", lambda slug, store, agent: False)
    monkeypatch.setattr(tick_module, "_remote_gone_reason", lambda slug, store, runtime, agent, now_ms: None)
    seen: list[tuple[object, ...]] = []

    def spying_watch_remote(
        slug: str, store: object, ledger: object, runtime: object, rows: object, agent: object, now_ms: int
    ) -> list[str]:
        seen.append((slug, store, ledger, runtime, rows, agent, now_ms))
        return ["remote"]

    monkeypatch.setattr(tick_module, "_watch_remote", spying_watch_remote)
    ledger, rows, runtime = object(), {}, _StubRuntime()
    agent = AgentRecord(name="agent-a", lane="eng", task="t1", state="working", runtime_backend="remote")
    judging = tick_module._Reaping("sw", store, ledger, runtime, rows, set(), 11)

    assert tick_module._reap_agent(judging, agent) == ["remote"]
    assert seen == [("sw", store, ledger, runtime, rows, agent, 11)]


def test_tick_hands_the_close_group_its_freshly_spawned_rows(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.tick import registry
    from scripts.tick.steps import tasks as task_steps_module
    from tests.helpers.seat import seat_owner
    from tests.swarm.test_tick import FakeRuntime, tasks
    from tests.swarm.test_tick_groups import _Marker

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    seen: dict[str, str] = {}

    def close(get: Any) -> list[str]:
        seen["claimed_by"] = get("rows")["t1"]["claimed_by"]
        return []

    fresh = registry.Registry()
    for step in (*registry.steps(), _Marker("close marker", task_steps_module.CLOSE, close)):
        fresh.register(step)
    monkeypatch.setattr(registry, "registry", fresh)
    seat_owner(store, ledger, runtime, "t1", 1)

    tick_module.tick("sw", store, ledger, runtime, 1)

    spawned = next(agent.name for agent in store.agents("sw") if agent.lane == "eng")
    assert seen["claimed_by"] == spawned


class _AnsweringRuntime:
    def observe(self, agent: AgentRecord) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(state="working")


def test_watch_remote_forwards_every_argument_to_watch_agent(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[object, ...]] = []

    def spying_watch_agent(
        slug: str, store: object, ledger: object, runtime: object, rows: object, agent: object, now_ms: int
    ) -> list[str]:
        seen.append((slug, store, ledger, runtime, rows, agent, now_ms))
        return ["watched"]

    monkeypatch.setattr(tick_module, "_watch_agent", spying_watch_agent)
    ledger, rows, runtime = object(), {}, _AnsweringRuntime()
    agent = AgentRecord(name="agent-a", lane="eng", task="t1", state="working", runtime_backend="remote")

    assert tick_module._watch_remote("sw", store, ledger, runtime, rows, agent, 13) == ["watched"]
    assert seen == [("sw", store, ledger, runtime, rows, agent, 13)]
