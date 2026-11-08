from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.swarm.store import AgentRecord
from scripts.tick.adapters.fakes import FakeLedger, FakeRuntime, fake_tools
from scripts.tick.deadline import Deadline
from scripts.tick.ports import Runtime
from scripts.tick.steps import agents

pytestmark = pytest.mark.unit


def test_runtime_fake_records_reap_operations_with_their_deadline() -> None:
    port = FakeRuntime({"live_names": {"eng"}, "retire": False, "refusal": "busy"})
    deadline = Deadline.after(10)
    agent = AgentRecord("eng", "eng", "task")
    homes = (Path("home"),)
    assert isinstance(port, Runtime)
    assert port.live_names(deadline=deadline) == {"eng"}
    assert port.observe(agent, deadline=deadline) is None
    assert port.retire(agent, homes=homes, deadline=deadline) is False
    assert port.name_pane(agent, deadline=deadline) is None
    assert port.refusal(agent, deadline=deadline) == "busy"
    assert [(call.operation, call.arguments, call.deadline) for call in port.calls] == [
        ("live_names", (), deadline),
        ("observe", (agent,), deadline),
        ("retire", (agent, homes), deadline),
        ("name_pane", (agent,), deadline),
        ("refusal", (agent,), deadline),
    ]


def test_ledger_fake_records_conditional_task_updates() -> None:
    port = FakeLedger({"update_task": {"id": "task", "state": "claimed"}})
    deadline = Deadline.after(10)
    fields = {"state": "claimed"}
    expected = {"claim_generation": 3}
    assert port.update_task("task", fields, if_state=("open",), if_match=expected, deadline=deadline) == {
        "id": "task", "state": "claimed"
    }
    assert [(call.operation, call.arguments, call.deadline) for call in port.calls] == [
        ("update_task", ("task", fields, ("open",), expected), deadline)
    ]


def test_reap_step_uses_only_declared_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = fake_tools()
    runtime = cast(FakeRuntime, tools.runtime)
    ledger = cast(FakeLedger, tools.ledger)
    runtime.answers.update(live_names={"eng"}, retire=False, refusal="busy")
    ledger.answers["update_task"] = {"id": "task"}
    deadline = Deadline.after(10)
    agent = AgentRecord("eng", "eng", "task")
    homes = (Path("scratch"),)
    state = {"slug": "sw", "store": object(), "rows": {}, "now_ms": 7, "deadline": deadline, "tools": tools}

    def reap(slug: str, store: object, client: Any, process: Any, rows: object, now: int, handed: Deadline) -> list[str]:
        assert (slug, store, rows, now, handed) == ("sw", state["store"], {}, 7, deadline)
        assert process.live_names() == {"eng"}
        process.observe(agent)
        assert process.retire(agent, homes=homes) is False
        process.name_pane(agent)
        assert process.refusal(agent) == "busy"
        assert client.update_task("sw", "task", {"state": "open"}, if_state=("claimed",), if_match={"x": 1}) == {
            "id": "task"
        }
        return ["reaped"]

    monkeypatch.setattr(agents, "_reap", reap)
    tick = SimpleNamespace(skip_refused=lambda call, *args: call(*args))
    assert agents._reap_step(tick, state.__getitem__) == ["reaped"]
    assert [call.operation for call in runtime.calls] == ["live_names", "observe", "retire", "name_pane", "refusal"]
    assert ledger.calls[0].arguments == ("task", {"state": "open"}, ("claimed",), {"x": 1})
    assert all(call.deadline is deadline for call in runtime.calls + ledger.calls)
