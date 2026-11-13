"""Export reads record their shape and caller, and operations writes their kinds, after the route."""

from collections import deque
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import request_metrics as metrics
from scripts.swarm_ledger.api import routes

EXPORT = "/api/v1/ledgers/:slug/export"


@pytest.fixture(autouse=True)
def samples(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "SAMPLES", deque(maxlen=64))
    monkeypatch.setattr(metrics, "JOURNAL", None)
    monkeypatch.setattr(metrics, "PENDING", [])


def handler(agent: str | None) -> SimpleNamespace:
    headers = {"X-Ledger-Agent": agent} if agent else {}
    return SimpleNamespace(command="POST", path="/api/v1/ledgers/one/export", raw_requestline=b"POST", headers=headers)


@pytest.mark.feature("ledger-export-shape-records")
@pytest.mark.parametrize(
    ("payload", "agent", "shape"),
    [
        ({}, None, "bare by anonymous"),
        ({"since": 0}, "swarm", "full by swarm"),
        ({"since": 7}, "swarm", "since by swarm"),
        ({"since": 7, "parts": ["tasks"], "closed": False}, "tick", "since parts scoped by tick"),
        ({"since": 0, "frozen": True}, "eng", "full scoped by eng"),
    ],
)
def test_an_export_read_names_its_shape_and_caller(payload: dict, agent: str | None, shape: str) -> None:
    assert routes.export_shape(handler(agent), payload) == shape


@pytest.mark.feature("ledger-export-shape-records")
def test_a_detailed_request_records_its_detail_after_the_route() -> None:
    def handle_one_request(request: SimpleNamespace) -> None:
        metrics.detailed(routes.export_shape(request, {"since": 0}))

    metrics.measured(handle_one_request)(handler("swarm"))
    assert [row[2] for row in metrics.SAMPLES] == [f"{EXPORT} full by swarm"]


@pytest.mark.feature("ledger-export-shape-records")
def test_a_request_without_detail_keeps_its_route_and_detail_outside_a_request_is_dropped() -> None:
    metrics.detailed("ignored")

    def handle_one_request(request: SimpleNamespace) -> None:
        return None

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
