"""The observe step's acknowledgement collection settles each waiting hive launch through the tick seams."""

from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import launch_job, readiness, tick
from scripts.swarm.store import EVENTS_ENGINE, SwarmError
from scripts.swarm_v2.runtime import codex
from scripts.tick.steps import observe

pytestmark = pytest.mark.unit

NOW = tick.LEASE_MS + 1_000


class _AckError(tick.SpawnError):
    """A collect error the fake tick seam reads as a pending hive acknowledgement."""


def _agent(name: str, state: str = "working", started_at: int = NOW - 10) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, task=f"task-{name}", lane="eng", seat=f"seat-{name}", state=state, started_at=started_at
    )


class _Store:
    """Answer the agent list and record every claim refresh."""

    def __init__(self, agents: list[SimpleNamespace]) -> None:
        self.listed, self.refreshed = agents, list[tuple[Any, ...]]()

    def agents(self, slug: str) -> list[SimpleNamespace]:
        assert slug == "sw"
        return self.listed

    def refresh(self, *args: Any) -> None:
        self.refreshed.append(args)


class _Runtime:
    """Answer collect and expire per agent name, recording each call."""

    def __init__(self, outcomes: dict[str, Any], expired: dict[str, Any]) -> None:
        self.outcomes, self.expired = outcomes, expired
        self.collected, self.expiring = list[tuple[Any, ...]](), list[tuple[Any, ...]]()

    def collect(self, config: Any, lane: str, name: str, task: dict[str, Any]) -> Any:
        self.collected.append((config, lane, name, task))
        outcome = self.outcomes[name]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def expire(self, config: Any, lane: str, name: str, task: dict[str, Any]) -> Any:
        self.expiring.append((config, lane, name, task))
        return self.expired[name]


def _seams(monkeypatch: pytest.MonkeyPatch, pending: set[str], waiting: set[str]) -> list[tuple[Any, ...]]:
    seen: list[tuple[Any, ...]] = []

    def collect(passed: Any) -> tuple[list[str], set[str]]:
        seen.append(("collect", passed))
        return ["collected"], set(pending)

    monkeypatch.setattr(launch_job, "collect", collect)
    monkeypatch.setattr(tick, "_Pass", lambda *args: ("pass", args))
    monkeypatch.setattr(
        readiness, "tick_launches", lambda engine: seen.append(("ready", engine)) or engine != EVENTS_ENGINE
    )
    monkeypatch.setattr(
        tick, "_acknowledging", lambda slug, store, agent: seen.append(("ack", slug, store)) or agent.name in waiting
    )
    monkeypatch.setattr(tick, "_acknowledging_error", lambda exc: isinstance(exc, _AckError))
    monkeypatch.setattr(tick, "placed_record", lambda agent, placed: ("record", agent.name, placed))
    monkeypatch.setattr(
        codex,
        "admit_legacy",
        lambda store, slug, record, runtime: seen.append(("admit", store, runtime)) or ("launched", slug, record),
    )
    monkeypatch.setattr(
        tick,
        "_finished_launch",
        lambda slug, store, doc, agent: seen.append(("finished", store, doc)) or f"finished {slug} {agent.name}",
    )
    monkeypatch.setattr(
        tick,
        "_started",
        lambda slug, config, store, ledger, doc, started, now_ms: (
            seen.append(("started", config, store, doc))
            or [
                f"started {slug} {ledger} {started.record.name} {started.task['id']} {started.placed} {started.launched} {now_ms}"
            ]
        ),
    )
    monkeypatch.setattr(
        tick,
        "_spawn_failed",
        lambda slug, store, ledger, runtime, rows, failed, now_ms: (
            seen.append(("failed", store, runtime))
            or [
                f"failed {slug} {ledger} {sorted(rows)} {failed.record.name} {failed.task['id']} {failed.error} {now_ms}"
            ]
        ),
    )
    monkeypatch.setattr(
        tick,
        "_expire_launch",
        lambda config, runtime, agent, error: runtime.expire(config, agent.lane, agent.name, {"error": str(error)}),
    )
    return seen


def _args(seen: list[tuple[Any, ...]], tag: str) -> list[tuple[Any, ...]]:
    return [entry[1:] for entry in seen if entry[0] == tag]


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_collected_acknowledgements_start_finish_fail_or_wait_each_launch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    expired = tick.SpawnError("lease ran out")
    agents = [
        _agent("a1"),
        _agent("a2", state="finished"),
        _agent("a3"),
        _agent("a4"),
        _agent("a5"),
        _agent("a6"),
        _agent("a7"),
        _agent("a8", started_at=NOW - tick.LEASE_MS),
        _agent("a9", started_at=NOW - tick.LEASE_MS + 1),
        _agent("a10"),
        _agent("a11", started_at=0),
        _agent("a12"),
    ]
    seen = _seams(
        monkeypatch, pending={"a3", "a5"}, waiting={name for name in (f"a{n}" for n in range(1, 13)) if name != "a4"}
    )
    runtime = _Runtime(
        {
            "a1": "placed-a1",
            "a2": "placed-a2",
            "a6": _AckError("pending"),
            "a7": tick.SpawnError("refused"),
            "a8": ValueError("lost"),
            "a9": ValueError("flaky"),
            "a10": SwarmError("conflict"),
            "a11": _AckError("still pending"),
            "a12": "placed-a12",
        },
        {"a8": expired, "a11": _AckError("kept")},
    )
    store = _Store(agents)
    config: Any = SimpleNamespace(engine="tick")
    tasks = [{"id": f"task-a{n}"} for n in range(1, 12)]
    doc = {"tasks": tasks}
    actions = observe._collect_acks("sw", config, store, "ledger", runtime, doc, NOW)
    rows = sorted({task["id"] for task in tasks})
    assert actions == [
        "collected",
        f"started sw ledger a1 task-a1 placed-a1 ('launched', 'sw', ('record', 'a1', 'placed-a1')) {NOW}",
        "finished sw a2",
        f"failed sw ledger {rows} a7 task-a7 refused {NOW}",
        f"failed sw ledger {rows} a8 task-a8 lease ran out {NOW}",
        f"failed sw ledger {rows} a10 task-a10 conflict {NOW}",
        f"started sw ledger a12 task-a12 placed-a12 ('launched', 'sw', ('record', 'a12', 'placed-a12')) {NOW}",
    ]
    assert seen[0] == (
        "collect",
        ("pass", ("sw", config, store, "ledger", runtime, {t["id"]: t for t in tasks}, doc, NOW)),
    )
    assert set(_args(seen, "ready")) == {("tick",)}
    assert set(_args(seen, "ack")) == {("sw", store)}
    assert _args(seen, "admit") == [(store, runtime), (store, runtime)]
    assert _args(seen, "finished") == [(store, doc)]
    assert _args(seen, "started") == [(config, store, doc), (config, store, doc)]
    assert _args(seen, "failed") == [(store, runtime)] * 3
    assert [call[2] for call in runtime.collected] == ["a1", "a2", "a6", "a7", "a8", "a9", "a10", "a11", "a12"]
    assert runtime.collected[0] == (config, "eng", "a1", {"id": "task-a1", "seat": "seat-a1", tick.ACK_LATER: True})
    assert runtime.expiring == [
        (config, "eng", "a8", {"error": "lost"}),
        (config, "eng", "a11", {"error": "still pending"}),
    ]
    assert store.refreshed == [
        ("sw", "task-a6", "a6", tick.LEASE_MS),
        ("sw", "task-a9", "a9", tick.LEASE_MS),
        ("sw", "task-a11", "a11", tick.LEASE_MS),
    ]
    assert capsys.readouterr().err == "a9 collect for task-a9 raised ValueError('flaky'), waiting for the hive\n"


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_the_events_engine_collects_no_hive_pass_and_no_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _seams(monkeypatch, pending={"a1"}, waiting={"a1"})
    runtime = _Runtime({"a1": "placed-a1"}, {})
    config: Any = SimpleNamespace(engine=EVENTS_ENGINE)
    actions = observe._collect_acks("sw", config, _Store([_agent("a1")]), "ledger", runtime, {"tasks": []}, NOW)
    assert actions == []
    assert _args(seen, "ready") == [(EVENTS_ENGINE,)]
    assert _args(seen, "collect") == []
    assert runtime.collected == []
