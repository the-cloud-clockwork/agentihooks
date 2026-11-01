from __future__ import annotations

from dataclasses import dataclass

import pytest

from scripts.tick.adapters import fakes
from scripts.tick.deadline import Deadline
from scripts.tick.ports import contracts
from scripts.tick.ports import tools as tool_contract

CASES = [
    ("ledger", "export", (), False),
    ("ledger", "mutate", ({},), True),
    ("coordination", "read", (), False),
    ("coordination", "lease", (), True),
    ("coordination", "lock", ("value",), True),
    ("coordination", "configure", ({},), True),
    ("coordination", "capacity", (), False),
    (
        "coordination",
        "mark",
        (
            "value",
            "value",
        ),
        True,
    ),
    ("source_control", "change", ("value",), False),
    ("source_control", "checks", ("value",), False),
    ("source_control", "runs", ("value",), False),
    ("source_control", "enqueue", ("value",), True),
    ("source_control", "merge", ("value",), True),
    ("source_control", "queue_health", (), False),
    ("source_control", "tags", (), False),
    (
        "source_control",
        "comment",
        (
            "value",
            "value",
        ),
        True,
    ),
    ("source_control", "capabilities", (), False),
    ("source_control", "issue_state", ("value",), False),
    ("source_control", "close_issue", ("value",), True),
    ("source_control", "rate_limit", (), False),
    ("source_control", "issues_enabled", (), False),
    ("source_control", "latest_runs", (), False),
    ("source_control", "cancel_run", ("value",), True),
    ("source_control", "merged_at", ("value",), False),
    ("runtime", "inspect", ("value",), False),
    ("runtime", "launch", ({},), True),
    ("runtime", "stop", ("value",), True),
    ("runtime", "panes", (), False),
    ("processes", "scan", (), False),
    ("inbox", "read", ("value",), False),
    (
        "inbox",
        "send",
        (
            "value",
            "value",
        ),
        True,
    ),
    ("inbox", "close", ("value", "done", "merged"), True),
    ("llm", "classify", ({},), True),
    ("metrics", "emit", ({},), True),
    ("host", "read", ("value",), False),
    ("host", "run", ({},), True),
]


@pytest.mark.parametrize("port,operation,args,write", CASES)
def test_every_fake_and_guarded_operation(port: str, operation: str, args: tuple[object, ...], write: bool) -> None:
    providers = fakes.fake_tools()
    assert isinstance(providers.ledger, fakes.FakeLedger)
    assert isinstance(providers.inbox, fakes.FakeInbox)
    if operation == "configure":
        from scripts.swarm.store import SwarmConfig

        args = (SwarmConfig("swarm", "repo", 1, 1),)
    fake = getattr(providers, port)
    expected = object()
    fake.answers[operation] = expected
    deadline = Deadline.after(10)
    restricted = providers.for_effects(frozenset())
    if write:
        with pytest.raises(contracts.UndeclaredEffect) as error:
            getattr(getattr(restricted, port), operation)(*args, deadline=deadline)
        assert str(error.value) == f"{port} is not a declared effect"
        assert fake.calls == []
    else:
        assert getattr(getattr(restricted, port), operation)(*args, deadline=deadline) is expected
        assert fake.calls.pop() == fakes.Call(operation, args, deadline)
    allowed = providers.for_effects(frozenset({port}))
    assert getattr(getattr(allowed, port), operation)(*args, deadline=deadline) is expected
    assert fake.calls == [fakes.Call(operation, args, deadline)]


@pytest.mark.parametrize(
    "port,protocol",
    [
        ("ledger", contracts.Ledger),
        ("coordination", contracts.Coordination),
        ("source_control", contracts.SourceControl),
        ("runtime", contracts.Runtime),
        ("processes", contracts.Processes),
        ("inbox", contracts.Inbox),
        ("llm", contracts.Llm),
        ("metrics", contracts.Metrics),
        ("host", contracts.Host),
    ],
)
def test_fake_satisfies_each_port_protocol(port: str, protocol: type) -> None:
    assert isinstance(getattr(fakes.fake_tools(), port), protocol)


def test_missing_or_invalid_deadline_is_refused() -> None:
    providers = fakes.fake_tools()
    assert isinstance(providers.ledger, fakes.FakeLedger)
    assert isinstance(providers.inbox, fakes.FakeInbox)
    tools = providers.for_effects(frozenset())
    for kwargs in ({}, {"deadline": None}, {"deadline": 1}):
        with pytest.raises(TypeError, match="^tools require a Deadline keyword argument$"):
            getattr(tools.ledger, "export")(**kwargs)
    assert providers.ledger.calls == []


def test_unknown_tool_is_refused() -> None:
    tools = fakes.fake_tools().for_effects(frozenset({"ledger"}))
    with pytest.raises(AttributeError, match="^undeclared_method$"):
        getattr(tools.ledger, "undeclared_method")


def test_restricting_tools_cannot_restore_an_effect() -> None:
    providers = fakes.fake_tools()
    assert isinstance(providers.ledger, fakes.FakeLedger)
    assert isinstance(providers.inbox, fakes.FakeInbox)
    tools = providers.for_effects(frozenset()).for_effects(frozenset({"inbox"}))
    with pytest.raises(contracts.UndeclaredEffect, match="^inbox is not a declared effect$"):
        tools.inbox.send("seat", "hello", deadline=Deadline.after(10))
    assert providers.inbox.calls == []


@dataclass(frozen=True)
class UndeclaredStep:
    effects: frozenset[str] = frozenset()

    def run(self, tools: tool_contract.Tools, deadline: Deadline) -> object:
        return tools.for_effects(self.effects).inbox.send("seat", "hello", deadline=deadline)


def test_step_cannot_act_through_an_undeclared_port() -> None:
    providers = fakes.fake_tools()
    assert isinstance(providers.ledger, fakes.FakeLedger)
    assert isinstance(providers.inbox, fakes.FakeInbox)
    with pytest.raises(contracts.UndeclaredEffect, match="^inbox is not a declared effect$"):
        UndeclaredStep().run(providers, Deadline.after(10))
    assert providers.inbox.calls == []


def test_fake_expired_deadline_and_default_answer() -> None:
    fake = fakes.FakeInbox()
    with pytest.raises(TimeoutError, match="^tool deadline expired$"):
        fake.send("seat", "hello", deadline=Deadline(0, lambda: 0))
    assert fake.calls == []
    deadline = Deadline.after(10)
    assert fake.send("seat", "hello", deadline=deadline) is None
    assert fake.calls == [fakes.Call("send", ("seat", "hello"), deadline)]


def test_guard_checks_deadline_before_calling_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    providers = fakes.fake_tools()
    assert isinstance(providers.ledger, fakes.FakeLedger)
    assert isinstance(providers.inbox, fakes.FakeInbox)
    seen: list[Deadline] = []

    def send(address: str, body: str, *, deadline: Deadline) -> object:
        seen.append(deadline)
        return None

    monkeypatch.setattr(providers.inbox, "send", send)
    tools = providers.for_effects(frozenset({"inbox"}))
    with pytest.raises(TimeoutError) as error:
        tools.inbox.send("seat", "hello", deadline=Deadline(0, lambda: 0))
    assert str(error.value) == "tool deadline expired"
    assert seen == []


@pytest.mark.parametrize("effects", [frozenset(), frozenset({"inbox"})])
def test_fake_factory_restricts_declared_effects(effects: frozenset[str]) -> None:
    tools = fakes.fake_tools(effects)
    deadline = Deadline.after(10)
    assert tools.ledger.export(deadline=deadline) == {}
    if "inbox" in effects:
        assert tools.inbox.send("seat", "hello", deadline=deadline) is None
    else:
        with pytest.raises(contracts.UndeclaredEffect, match="^inbox is not a declared effect$"):
            tools.inbox.send("seat", "hello", deadline=deadline)
