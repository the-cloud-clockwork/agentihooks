from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

import pytest

from scripts.swarm import lease
from scripts.swarm_ledger.api.client import ResourceClient
from scripts.tick.adapters import ledger as ledger_adapter
from scripts.tick.deadline import Deadline
from scripts.tick.ports import Ledger

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore

pytestmark = pytest.mark.unit


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def export(self, slug: str) -> dict[str, object]:
        self.calls.append(("export", slug, None))
        return {"tasks": []}

    def mutate(self, slug: str, operations: list[dict[str, object]]) -> dict[str, object]:
        self.calls.append(("mutate", slug, operations))
        return {"applied": len(operations)}


def _ledger() -> tuple[ledger_adapter.ClientLedger, _Client, list[float]]:
    client, timeouts = _Client(), []

    def clients(remaining: float) -> ResourceClient:
        timeouts.append(remaining)
        return cast(ResourceClient, client)

    return ledger_adapter.ClientLedger("sw", clients), client, timeouts


def _clock(now: list[float]) -> Callable[[], float]:
    return lambda: now[0]


def test_the_client_ledger_answers_the_ledger_port() -> None:
    adapter, _, _ = _ledger()
    assert isinstance(adapter, Ledger)


def test_export_reads_the_export_route_bounded_by_the_deadline_left() -> None:
    adapter, client, timeouts = _ledger()
    now = [10.0]
    assert adapter.export(deadline=Deadline(13.5, _clock(now))) == {"tasks": []}
    assert client.calls == [("export", "sw", None)]
    assert timeouts == [3.5]


def test_mutate_sends_the_operations_through_the_mutate_route_unstamped_outside_a_pass() -> None:
    adapter, client, timeouts = _ledger()
    operations = [{"op": "add", "text": "hello"}]
    assert adapter.mutate(operations, deadline=Deadline(5.0, _clock([4.0]))) == {"applied": 1}
    assert client.calls == [("mutate", "sw", [{"op": "add", "text": "hello"}])]
    assert operations == [{"op": "add", "text": "hello"}]
    assert timeouts == [1.0]


def test_mutate_stamps_each_operation_with_the_epoch_of_its_ledger() -> None:
    adapter, client, _ = _ledger()
    with lease.fencing(7):
        adapter.mutate([{"op": "add"}, {"op": "close"}], deadline=Deadline(5.0, _clock([0.0])))
    assert client.calls == [
        ("mutate", "sw", [{"op": "add", "controller_epoch": 7}, {"op": "close", "controller_epoch": 7}])
    ]


def test_mutate_inside_a_pass_stamps_the_pass_epoch_of_its_own_swarm() -> None:
    adapter, client, _ = _ledger()
    token = lease.PASS.set((cast("RedisStore", object()), "sw", 12))
    try:
        adapter.mutate([{"op": "add"}], deadline=Deadline(5.0, _clock([0.0])))
    finally:
        lease.PASS.reset(token)
    assert client.calls == [("mutate", "sw", [{"op": "add", "controller_epoch": 12}])]


@pytest.mark.parametrize("operation", ["export", "mutate"])
def test_an_expired_deadline_reaches_no_route(operation: str) -> None:
    adapter, client, timeouts = _ledger()
    expired = Deadline(5.0, _clock([5.0]))
    with pytest.raises(TimeoutError, match="^tool deadline expired$"):
        if operation == "export":
            adapter.export(deadline=expired)
        else:
            adapter.mutate([{"op": "add"}], deadline=expired)
    assert client.calls == []
    assert timeouts == []


def test_resource_client_bounds_each_request_by_the_ledger_timeout_and_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from scripts.swarm import ledger_client

    seen: list[tuple[str, bool]] = []

    def credentials(slug: str, service: bool) -> dict[str, str]:
        seen.append((slug, service))
        return {"X-Ledger-Agent": "swarm"}

    fake = SimpleNamespace(base=lambda slug: f"http://ledger/{slug}", credentials=credentials, REQUEST_TIMEOUT=10)
    monkeypatch.setattr(ledger_client, "_ledger", lambda: fake)
    build = ledger_adapter.resource_client("sw", service=True)
    short, long = build(2.5), build(30.0)
    assert (short.base, short.timeout) == ("http://ledger/sw", 2.5)
    assert (long.base, long.timeout) == ("http://ledger/sw", 10)
    assert seen == [("sw", True), ("sw", True)]
    assert ledger_adapter.resource_client("sw")(1.0).timeout == 1.0
    assert seen[-1] == ("sw", False)
