"""Export reads record their shape and caller, and operations writes their kinds, after the route."""

from collections import deque
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest

from scripts.swarm_ledger import request_metrics as metrics
from scripts.swarm_ledger.api import routes
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.api.services import Services

EXPORT = "/api/v1/ledgers/:slug/export"


@pytest.fixture(autouse=True)
def samples(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "SAMPLES", deque(maxlen=64))
    monkeypatch.setattr(metrics, "JOURNAL", None)
    monkeypatch.setattr(metrics, "PENDING", [])


def handler(agent: str | None, **headers: str) -> SimpleNamespace:
    named = {"X-Ledger-Agent": agent} if agent else {}
    return SimpleNamespace(
        command="POST", path="/api/v1/ledgers/one/export", raw_requestline=b"POST", headers=named | headers
    )


@pytest.mark.feature("ledger-export-shape-records")
@pytest.mark.parametrize(
    ("payload", "agent", "shape"),
    [
        ({}, None, "bare by -"),
        ({"since": 0}, "swarm", "full by swarm"),
        ({"since": 7}, "ci@323133-4175", "since by ci@323133-4175"),
        (
            {"since": 7, "parts": ["tasks"], "closed": False, "events_after": 3},
            "eng",
            "since parts scoped events by eng",
        ),
        ({"since": 0, "frozen": True}, "eng", "full scoped by eng"),
        ([], "eng", "refused by eng"),
        ({"since": 7}, "two words", "since by -"),
        ({"since": 7}, "x" * 65, "since by -"),
    ],
)
def test_an_export_read_names_its_shape_and_caller(payload: object, agent: str | None, shape: str) -> None:
    assert routes.export_shape(handler(agent), payload) == shape


@pytest.mark.feature("ledger-export-shape-records")
def test_a_tick_export_read_is_named_by_the_server_request_caller() -> None:
    assert routes.export_shape(handler(None, **{"X-Ledger-Tick": "1"}), {"since": 7}) == "since by tick"


@pytest.mark.feature("ledger-export-shape-records")
def test_a_detailed_request_records_its_detail_after_the_route() -> None:
    def handle_one_request(request: SimpleNamespace) -> None:
        metrics.detailed(routes.export_shape(request, {"since": 0}))

    metrics.measured(handle_one_request)(handler("swarm"))
    assert [row[2] for row in metrics.SAMPLES] == [f"{EXPORT} full by swarm"]


@pytest.mark.feature("ledger-export-shape-records")
def test_a_request_without_detail_keeps_its_route_and_detail_outside_a_request_is_dropped() -> None:
    metrics.detailed("ignored")

    def handle_one_request(_: SimpleNamespace) -> None:
        pass

    metrics.measured(handle_one_request)(handler(None))
    assert [row[2] for row in metrics.SAMPLES] == [EXPORT]


@pytest.mark.feature("ledger-export-shape-records")
@pytest.mark.parametrize(
    ("payload", "kinds"),
    [
        ({"ops": [{"op": "set"}, {"op": "add"}, {"op": "set"}]}, "add+set"),
        ({"ops": [{"op": "time_left"}, "junk"]}, "time_left"),
        ({"ops": "junk"}, ""),
    ],
)
def test_an_operations_write_names_its_distinct_operation_kinds(payload: dict, kinds: str) -> None:
    assert routes.operation_kinds(payload) == kinds


def measured_route(run: Callable[[SimpleNamespace], object], request: SimpleNamespace) -> list[str]:
    def handle_one_request(handler: SimpleNamespace) -> None:
        run(handler)

    metrics.measured(handle_one_request)(request)
    return [row[2] for row in metrics.SAMPLES]


def exporting(request: SimpleNamespace) -> SimpleNamespace:
    request.send_export = lambda body: body
    return request


@pytest.mark.feature("ledger-export-shape-records")
@pytest.mark.parametrize(
    ("payload", "shape"), [({}, "bare by eng"), ({"since": 4, "parts": ["tasks"]}, "since parts by eng")]
)
def test_the_export_route_records_the_shape_of_each_read(payload: dict, shape: str) -> None:
    repository = SimpleNamespace(export_since=lambda *_: (5, b'{"data":{}}'))
    services = cast(Services, SimpleNamespace(repository=repository))
    rows = measured_route(
        lambda request: routes.export(request, services, "one", "export", payload), exporting(handler("eng"))
    )
    assert rows == [f"{EXPORT} {shape}"]


@pytest.mark.feature("ledger-export-shape-records")
def test_a_refused_export_read_is_still_recorded_with_its_shape() -> None:
    services = cast(Services, SimpleNamespace())

    def refused(request: SimpleNamespace) -> None:
        with pytest.raises(APIError):
            routes.export(request, services, "one", "export", {"since": -1})

    assert measured_route(refused, handler("eng")) == [f"{EXPORT} since by eng"]


@pytest.mark.feature("ledger-export-shape-records")
def test_an_operations_write_records_its_operation_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm_ledger.api import mutations

    monkeypatch.setattr(routes, "body", lambda *_: {"ops": [{"op": "set"}, {"op": "add"}]})
    monkeypatch.setattr(mutations, "apply", lambda *_: {"ok": True})
    monkeypatch.setattr(mutations, "writer", lambda _: None)
    request = handler("eng")
    request.path = "/api/v1/ledgers/one/operations"
    services = cast(Services, SimpleNamespace())
    rows = measured_route(lambda item: routes.ledger_operation(item, services, "one", "operations", "eng"), request)
    assert rows == ["/api/v1/ledgers/:slug/operations add+set"]
