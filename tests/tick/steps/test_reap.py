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
