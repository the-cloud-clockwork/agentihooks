"""The ledger comment write and the process table read: their facades, adapters, fakes and declared effects."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.tick.adapters import ledger as module
from scripts.tick.adapters.calls import LedgerCalls, MetricsCalls
from scripts.tick.adapters.fakes import FakeLedger, FakeMetrics, FakeProcesses, fake_tools
from scripts.tick.adapters.ledger import ClientLedger, PassLedger
from scripts.tick.deadline import Deadline
from scripts.tick.ports import UndeclaredEffect

pytestmark = pytest.mark.unit

EXPIRED = Deadline(0, lambda: 0)


def test_the_ledger_facade_comments_through_the_port_with_its_deadline() -> None:
    port = FakeLedger()
    deadline = Deadline(10, lambda: 0)

    assert LedgerCalls(port, deadline).comment("sw", "task", "blocked here", "swarm") is None

    assert [(call.operation, call.args, call.deadline) for call in port.calls] == [
        ("comment", ("task", "blocked here", "swarm"), deadline)
    ]


def test_the_pass_ledger_comments_through_the_domain_client_within_its_deadline() -> None:
    seen: list[tuple[object, ...]] = []
    adapter = PassLedger("sw", SimpleNamespace(comment=lambda *values: seen.append(values)))

    adapter.comment("task", "blocked here", "swarm", deadline=Deadline(10, lambda: 0))
    with pytest.raises(TimeoutError):
        adapter.comment("task", "late", "swarm", deadline=EXPIRED)

    assert seen == [("sw", "task", "blocked here", "swarm")]


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


def test_a_ledger_comment_is_a_write_that_needs_the_ledger_effect() -> None:
    tools = fake_tools()

    with pytest.raises(UndeclaredEffect) as error:
        tools.for_effects(frozenset()).ledger.comment("task", "text", "swarm", deadline=Deadline.after(10))
    tools.for_effects(frozenset({"ledger"})).ledger.comment("task", "text", "swarm", deadline=Deadline.after(10))

    assert str(error.value) == "ledger is not a declared effect"
    assert [call.operation for call in cast(FakeLedger, tools.ledger).calls] == ["comment"]


def test_metrics_calls_table_reads_the_process_table_through_the_processes_port() -> None:
    deadline = Deadline(10, lambda: 0)
    processes = FakeProcesses(answers={"rescan": {41: "agent"}})
    calls = MetricsCalls(FakeMetrics(), processes, deadline)

    assert calls.table() == {41: "agent"}

    assert [(call.operation, call.args, call.deadline) for call in processes.calls] == [("rescan", (), deadline)]
