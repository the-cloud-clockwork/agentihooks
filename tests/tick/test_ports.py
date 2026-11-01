from __future__ import annotations

import pytest

from scripts.tick import ports
from scripts.tick.adapters import fakes
from scripts.tick.deadline import Deadline


@pytest.mark.parametrize(
    "port,operation",
    [
        ("ledger", "mutate"),
        ("coordination", "mark"),
        ("source_control", "merge"),
        ("runtime", "launch"),
        ("inbox", "send"),
        ("llm", "classify"),
        ("metrics", "emit"),
        ("host", "run"),
    ],
)
def test_undeclared_step_effect_refuses_before_adapter(port: str, operation: str) -> None:
    providers = fakes.fake_tools()
    tools = providers.for_effects(frozenset())
    with pytest.raises(ports.UndeclaredEffect, match=f"^{port} is not a declared effect$"):
        getattr(getattr(tools, port), operation)("request", deadline=Deadline.after(10))
    assert getattr(providers, port).calls == []


def test_read_tools_remain_available_and_forward_deadline() -> None:
    providers = fakes.fake_tools()
    deadline = Deadline.after(10)
    tools = providers.for_effects(frozenset())
    assert tools.ledger.export(deadline=deadline) == {}
    assert providers.ledger.calls[0].deadline is deadline
    assert tools.processes.scan(deadline=deadline) == ()


def test_declared_effect_calls_adapter_with_deadline() -> None:
    providers = fakes.fake_tools()
    deadline = Deadline.after(10)
    tools = providers.for_effects(frozenset({"inbox"}))
    assert tools.inbox.send("seat", "hello", deadline=deadline) is None
    call = providers.inbox.calls[0]
    assert (call.operation, call.args, call.deadline) == ("send", ("seat", "hello"), deadline)


def test_unsupported_is_provider_answer() -> None:
    providers = fakes.fake_tools()
    answer = providers.source_control.queue_health(deadline=Deadline.after(10))
    assert answer == ports.Unsupported("queue_health")


def test_expired_deadline_refuses_before_adapter() -> None:
    providers = fakes.fake_tools()
    tools = providers.for_effects(frozenset({"inbox"}))
    with pytest.raises(TimeoutError, match="^tool deadline expired$"):
        tools.inbox.send("seat", "hello", deadline=Deadline(0, lambda: 0))
    assert providers.inbox.calls == []
