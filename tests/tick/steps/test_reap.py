from typing import Any, cast

import pytest

from scripts.hub import reconciler
from scripts.swarm.store import AgentRecord
from scripts.tick.adapters.fakes import FakeRuntime, fake_tools
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps import agents

pytestmark = pytest.mark.unit


class _View:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    def read(self, domain: Domain, key: str) -> Any:
        return self.state[key]


@pytest.mark.feature("swarm-tick-agents-reap")
def test_reap(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = fake_tools()
    runtime = cast(FakeRuntime, tools.runtime)
    runtime.answers["live_names"] = set()
    agent = AgentRecord("starting-master", "master", "", state="starting")
    monkeypatch.setattr(reconciler, "legacy_agents", lambda store, slug: [agent])
    state = {"slug": "sw", "store": object(), "rows": {}, "now_ms": 7}
    step = next(step for step in agents.STEPS if step.name == "reap")
    deadline = Deadline.after(10)
    result = step.run(Context(_View(state), deadline, tools))
    assert [write.value for write in result.writes] == [[]]
    assert [(call.operation, call.args, call.deadline) for call in runtime.calls] == [("live_names", (), deadline)]


@pytest.mark.feature("swarm-tick-agents-reap")
def test_reap_refuses_a_guarded_ownership_write_without_watching_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from scripts.swarm import tick
    from scripts.tick.adapters.ledger import ClientLedger

    agent = AgentRecord("eng", "eng", "task")
    row = {"id": "task", "state": "open", "claimed_by": "", "claim_generation": 1}
    tools = fake_tools()
    runtime = cast(FakeRuntime, tools.runtime)
    runtime.answers["live_names"] = {"eng"}
    ledger = ClientLedger(
        "sw", lambda remaining: cast(Any, SimpleNamespace(mutate=lambda slug, ops: {"rejected": ["task_update"]}))
    )
    tools = replace(tools, ledger=ledger)
    monkeypatch.setattr(reconciler, "legacy_agents", lambda store, slug: [agent])
    monkeypatch.setattr(tick, "_acknowledging", lambda *args: False)
    monkeypatch.setattr(tick, "_remote_gone_reason", lambda *args: "")
    monkeypatch.setattr(tick, "_ended", lambda *args: False)
    monkeypatch.setattr(tick, "_current_owner", lambda *args: True)
    watched: list[bool] = []
    monkeypatch.setattr(tick, "_watch_agent", lambda *args: watched.append(True) or [])
    store = SimpleNamespace(ensure_claim=lambda *args: True)
    state = {"slug": "sw", "store": store, "rows": {"task": row}, "now_ms": 7}
    step = next(step for step in agents.STEPS if step.name == "reap")
    result = step.run(Context(_View(state), Deadline.after(10), tools))
    assert [write.value for write in result.writes] == [
        ["skipped scripts.tick.steps.agents._reap: the ledger refused its write"]
    ]
    assert watched == []
    assert row == {"id": "task", "state": "open", "claimed_by": "", "claim_generation": 1}


@pytest.mark.feature("swarm-tick-agents-reap")
def test_reap_skips_one_agents_identity_conflict_and_finishes_the_rest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import tick
    from scripts.swarm.store import SwarmError

    stale = AgentRecord("stale-eng", "eng", "task-a")
    fine = AgentRecord("fine-eng", "eng", "task-b")
    tools = fake_tools()
    runtime = cast(FakeRuntime, tools.runtime)
    runtime.answers["live_names"] = set()
    monkeypatch.setattr(reconciler, "legacy_agents", lambda store, slug: [stale, fine])

    def fake_reap_agent(judging: Any, agent: AgentRecord) -> list[str]:
        if agent.name == "stale-eng":
            raise SwarmError("stale or changed execution identity")
        return [f"watched {agent.name}"]

    monkeypatch.setattr(tick, "_reap_agent", fake_reap_agent)
    state = {"slug": "sw", "store": object(), "rows": {}, "now_ms": 7}
    step = next(step for step in agents.STEPS if step.name == "reap")
    result = step.run(Context(_View(state), Deadline.after(10), tools))
    actions = cast(list[str], result.writes[0].value)
    assert actions == ["skipped stale-eng, its execution identity changed mid pass", "watched fine-eng"]
    err = capsys.readouterr().err
    assert "reap skipped stale-eng, its execution identity changed mid pass: stale or changed execution identity" in err


@pytest.mark.feature("swarm-tick-agents-reap")
def test_reap_keeps_an_agent_whose_drop_the_store_refuses_for_the_next_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from scripts.swarm import tick
    from scripts.tick.steps import common

    lost = AgentRecord("lost-eng", "eng", "task-a")
    fine = AgentRecord("fine-eng", "eng", "task-b")
    rows = {"task-a": {"id": "task-a", "state": "claimed", "claimed_by": "lost-eng", "claim_generation": 1}}
    released: list[str] = []
    store = SimpleNamespace(
        claims=lambda slug, task: 0,
        claim_generation=lambda slug, task: 1,
        release=lambda slug, task, name: released.append(name),
        drop_agent=lambda slug, name: False,
    )
    updates: list[str] = []
    ledger = SimpleNamespace(update_task=lambda slug, task, *args, **kwargs: updates.append(task) or {"state": "open"})
    settled: list[str] = []
    monkeypatch.setattr(common, "_settle_exit", lambda store, name, seat: settled.append(name))
    monkeypatch.setattr(reconciler, "legacy_agents", lambda store, slug: [lost, fine])

    def fake_reap_agent(judging: Any, agent: AgentRecord) -> list[str]:
        if agent.name == "lost-eng":
            return [f"lost {agent.name}" + tick._drop(judging.slug, judging.store, judging.ledger, judging.rows, agent)]
        return [f"watched {agent.name}"]

    monkeypatch.setattr(tick, "_reap_agent", fake_reap_agent)
    runtime = SimpleNamespace(live_names=lambda: set())
    actions = agents._reap("sw", store, ledger, runtime, rows, 7)
    assert actions == ["kept lost-eng, the store refused its drop, retrying next pass", "watched fine-eng"]
    assert updates == []
    assert settled == []
    assert rows["task-a"]["claimed_by"] == "lost-eng"
