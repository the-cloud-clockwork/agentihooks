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
