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
    assert [(call.operation, call.args, call.deadline) for call in port.calls] == [
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
        "id": "task",
        "state": "claimed",
    }
    assert [(call.operation, call.args, call.deadline) for call in port.calls] == [
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

    def reap(
        slug: str, store: object, client: Any, process: Any, rows: object, now: int, handed: Deadline
    ) -> list[str]:
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
    assert ledger.calls[0].args == ("task", {"state": "open"}, ("claimed",), {"x": 1})
    assert all(call.deadline is deadline for call in runtime.calls + ledger.calls)


@pytest.mark.parametrize(
    "operation,args,expected",
    [
        ("live_names", (), {"eng"}),
        ("observe", (AgentRecord("eng", "eng", "task"),), "observed"),
        ("retire", (AgentRecord("eng", "eng", "task"),), False),
        ("name_pane", (AgentRecord("eng", "eng", "task"),), None),
        ("refusal", (AgentRecord("eng", "eng", "task"),), "busy"),
        ("resume", ("config", AgentRecord("eng", "eng", "task"), "continue"), "placed"),
        ("dismiss", (AgentRecord("eng", "eng", "task"),), True),
    ],
)
def test_real_runtime_forwards_reap_operations_and_refuses_expired_deadlines(
    operation: str, args: tuple[object, ...], expected: object
) -> None:
    from scripts.tick.adapters.runtime import RouterRuntime

    seen: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def call(*passed: object, **keywords: object) -> object:
        seen.append((passed, keywords))
        return expected

    adapter = RouterRuntime(cast(Any, object()), lambda name: None, legacy=SimpleNamespace(**{operation: call}))
    deadline = Deadline(10, lambda: 0)
    keywords = {"homes": (Path("scratch"),)} if operation == "retire" else {}
    assert getattr(adapter, operation)(*args, **keywords, deadline=deadline) == expected
    assert seen == [(args, keywords)]
    with pytest.raises(TimeoutError) as error:
        getattr(adapter, operation)(*args, **keywords, deadline=Deadline(0, lambda: 0))
    assert str(error.value) == "tool deadline expired"
    assert seen == [(args, keywords)]


@pytest.mark.parametrize("states,match", [((), None), (("open",), {}), (("pr", "claimed"), {"claim_generation": 4})])
def test_real_ledger_task_updates_keep_every_guard(
    monkeypatch: pytest.MonkeyPatch, states: tuple[str, ...], match: dict[str, object] | None
) -> None:
    from scripts.tick.adapters import ledger as module
    from scripts.tick.adapters.ledger import ClientLedger

    sent: list[tuple[str, object]] = []
    timeouts: list[float] = []
    row = {"id": "task", "state": "claimed"}

    def mutate(slug: str, operations: object) -> dict[str, object]:
        sent.append((slug, operations))
        return {"tasks": [{"id": "other"}, row]}

    def client(timeout: float) -> Any:
        timeouts.append(timeout)
        return SimpleNamespace(mutate=mutate)

    monkeypatch.setattr(module, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    adapter = ClientLedger("sw", client)
    fields = {"state": "claimed"}
    assert adapter.update_task("task", fields, if_state=states, if_match=match, deadline=Deadline(10, lambda: 2)) == row
    guard: dict[str, object] = {"if_state": list(states)} if states else {}
    if match is not None:
        guard["if_match"] = match
    assert sent == [
        (
            "sw",
            [
                {
                    "op": "task_update",
                    "id": "task_update-aaaaaaaaaa",
                    "by": "swarm",
                    "item": "tasks/task",
                    "fields": fields,
                    **guard,
                }
            ],
        )
    ]
    assert timeouts == [8]
    assert adapter.update_task("missing", {}, deadline=Deadline(10, lambda: 2)) == {}
    before = list(sent)
    with pytest.raises(TimeoutError):
        adapter.update_task("task", fields, deadline=Deadline(0, lambda: 0))
    assert sent == before


@pytest.mark.parametrize(
    "operation,args,write",
    [
        ("live_names", (), False),
        ("observe", (AgentRecord("eng", "eng", "task"),), False),
        ("refusal", (AgentRecord("eng", "eng", "task"),), False),
        ("retire", (AgentRecord("eng", "eng", "task"),), True),
        ("name_pane", (AgentRecord("eng", "eng", "task"),), True),
        ("resume", ("config", AgentRecord("eng", "eng", "task"), "resume"), True),
        ("dismiss", (AgentRecord("eng", "eng", "task"),), True),
    ],
)
def test_reap_runtime_operations_obey_effects_and_deadlines(
    operation: str, args: tuple[object, ...], write: bool
) -> None:
    from scripts.tick.ports import UndeclaredEffect

    tools = fake_tools()
    fake = cast(FakeRuntime, tools.runtime)
    fake.answers[operation] = "answer"
    deadline = Deadline(10, lambda: 0)
    read_only = tools.for_effects(frozenset())
    if write:
        with pytest.raises(UndeclaredEffect) as error:
            getattr(read_only.runtime, operation)(*args, deadline=deadline)
        assert str(error.value) == "runtime is not a declared effect"
        assert fake.calls == []
    else:
        assert getattr(read_only.runtime, operation)(*args, deadline=deadline) == "answer"
        fake.calls.clear()
    getattr(tools.for_effects(frozenset({"runtime"})).runtime, operation)(*args, deadline=deadline)
    assert len(fake.calls) == 1
    assert fake.calls[0].deadline is deadline
    with pytest.raises(TimeoutError):
        getattr(fake, operation)(*args, deadline=Deadline(0, lambda: 0))
    assert len(fake.calls) == 1


def test_reap_ledger_facade_preserves_reads_and_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.tick.adapters import ledger as module
    from scripts.tick.adapters.calls import LedgerCalls, RuntimeCalls

    monkeypatch.setattr(module, "uuid4", lambda: SimpleNamespace(hex="b" * 32))
    port = FakeLedger({"export": {"tasks": [{"id": "other"}, {"id": "task", "state": "open"}]}})
    deadline = Deadline(10, lambda: 0)
    ledger = LedgerCalls(port, deadline)
    assert ledger.task("sw", "task") == {"id": "task", "state": "open"}
    assert ledger.task("sw", "missing") == {}
    ledger.notify("sw", "notice")
    ledger.question("sw", "question", "eng", "question-id")
    assert [(call.operation, call.args, call.deadline) for call in port.calls] == [
        ("export", (), deadline),
        ("export", (), deadline),
        ("mutate", ([{"op": "notice", "id": "notice-bbbbbbbbbb", "by": "swarm", "text": "notice"}],), deadline),
        (
            "mutate",
            ([{"op": "add_item", "id": "question-id", "by": "eng", "list": "questions", "text": "question"}],),
            deadline,
        ),
    ]
    runtime = FakeRuntime({"resume": "placed", "dismiss": True})
    facade = RuntimeCalls(runtime, deadline)
    agent = AgentRecord("eng", "eng", "task")
    config = cast(Any, "config")
    assert facade.resume(config, agent, "continue") == "placed"
    assert facade.dismiss(agent) is True
    assert [(call.operation, call.args, call.deadline) for call in runtime.calls] == [
        ("resume", (config, agent, "continue"), deadline),
        ("dismiss", (agent,), deadline),
    ]


def test_reap_ledger_update_requires_its_effect() -> None:
    from scripts.tick.ports import UndeclaredEffect

    tools = fake_tools()
    with pytest.raises(UndeclaredEffect) as error:
        tools.for_effects(frozenset()).ledger.update_task("task", {}, deadline=Deadline.after(10))
    assert str(error.value) == "ledger is not a declared effect"
    assert cast(FakeLedger, tools.ledger).calls == []


def test_pass_binds_the_fenced_runtime_to_the_port(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import cli, controller
    from scripts.tick.adapters.runtime import RouterRuntime
    from scripts.tick.ports.tools import PASS_TOOLS

    raw, fenced, config, held = object(), object(), object(), object()
    seen: list[tuple[object, ...]] = []

    def fence(*args: object) -> object:
        seen.append(args)
        return fenced

    monkeypatch.setattr(controller, "FencedRuntime", fence)
    monkeypatch.setattr(controller, "deployment_for", lambda found, environ: "deployment" if found is config else None)
    store = SimpleNamespace(config=lambda slug: config if slug == "sw" else None)
    wrapper = SimpleNamespace(store=store, slug="sw", held=held, state={"runtime": raw})
    tools = fake_tools()
    runtime = RouterRuntime(cast(Any, object()), lambda name: None)
    from dataclasses import replace

    token = PASS_TOOLS.set(replace(tools, runtime=runtime))
    try:
        assert cli._Wrapper._fence(wrapper) is True
        assert runtime.legacy is fenced
    finally:
        PASS_TOOLS.reset(token)
    assert wrapper.state == {"runtime": fenced}
    assert seen == [(store, "sw", held, raw, "deployment")]
    wrapper.state["runtime"] = raw
    assert cli._Wrapper._fence(wrapper) is True
    assert wrapper.state == {"runtime": fenced}
