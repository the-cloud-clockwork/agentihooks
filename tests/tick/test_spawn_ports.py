"""The ledger comment operation the spawn step writes through: its facade, adapters, fake and declared effect."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.tick.adapters import ledger as module
from scripts.tick.adapters.calls import LedgerCalls
from scripts.tick.adapters.fakes import FakeLedger, fake_tools
from scripts.tick.adapters.ledger import ClientLedger, PassLedger
from scripts.tick.deadline import Deadline
from scripts.tick.ports import UndeclaredEffect

pytestmark = pytest.mark.unit

EXPIRED = Deadline(0, lambda: 0)


@pytest.mark.feature("swarm-tick-spawn")
def test_the_ledger_facade_comments_through_the_port_with_its_deadline() -> None:
    port = FakeLedger()
    deadline = Deadline(10, lambda: 0)

    assert LedgerCalls(port, deadline).comment("sw", "task", "blocked here", "swarm") is None

    assert [(call.operation, call.args, call.deadline) for call in port.calls] == [
        ("comment", ("task", "blocked here", "swarm"), deadline)
    ]


@pytest.mark.feature("swarm-tick-spawn")
def test_the_pass_ledger_comments_through_the_domain_client_within_its_deadline() -> None:
    seen: list[tuple[object, ...]] = []
    adapter = PassLedger("sw", SimpleNamespace(comment=lambda *values: seen.append(values)))

    adapter.comment("task", "blocked here", "swarm", deadline=Deadline(10, lambda: 0))
    with pytest.raises(TimeoutError):
        adapter.comment("task", "late", "swarm", deadline=EXPIRED)

    assert seen == [("sw", "task", "blocked here", "swarm")]


@pytest.mark.feature("swarm-tick-spawn")
def test_the_resource_ledger_adds_the_comment_to_the_task_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[object, ...]] = []
    monkeypatch.setattr(module, "uuid4", lambda: SimpleNamespace(hex="c" * 32))

    def mutate(*args: object) -> dict[str, object]:
        sent.append(args)
        return {}

    adapter = ClientLedger("sw", lambda remaining: cast(Any, SimpleNamespace(mutate=mutate)))
    adapter.comment("task", "blocked here", "eng", deadline=Deadline.after(10))
    with pytest.raises(TimeoutError):
        adapter.comment("task", "late", "eng", deadline=EXPIRED)

    assert sent == [
        (
            "sw",
            [
                {
                    "op": "add",
                    "id": "add-cccccccccc",
                    "by": "eng",
                    "thread": "tasks/task/comments",
                    "text": "blocked here",
                }
            ],
        )
    ]


@pytest.mark.feature("swarm-tick-spawn")
def test_a_ledger_comment_is_a_write_that_needs_the_ledger_effect() -> None:
    tools = fake_tools()

    with pytest.raises(UndeclaredEffect) as error:
        tools.for_effects(frozenset()).ledger.comment("task", "text", "swarm", deadline=Deadline.after(10))
    tools.for_effects(frozenset({"ledger"})).ledger.comment("task", "text", "swarm", deadline=Deadline.after(10))

    assert str(error.value) == "ledger is not a declared effect"
    assert [call.operation for call in cast(FakeLedger, tools.ledger).calls] == ["comment"]


@pytest.mark.feature("swarm-tick-spawn")
def test_unresolved_tasks_stop_writing_when_the_pass_deadline_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import tick
    from scripts.tick.steps import place

    clock = SimpleNamespace(now=0.0)
    deadline = Deadline(1.0, lambda: clock.now)
    calls: list[str] = []

    def unresolved(slug: str, ledger: Any, rows: dict[str, Any], task_id: str, reason: str) -> str:
        calls.append(task_id)
        clock.now = 1.0
        return task_id

    monkeypatch.setattr(tick, "_unresolved", unresolved)
    order = [("eng", {"id": task_id, place.profile_first.UNRESOLVED: "reason"}) for task_id in ("t1", "t2")]
    token = place.PASS_DEADLINE.set(deadline)
    try:
        assert place._block_unresolved("sw", None, {}, order) == ["t1"]
    finally:
        place.PASS_DEADLINE.reset(token)

    assert calls == ["t1"]
