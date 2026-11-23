"""The ledger API serves the open operator orders over http."""

from typing import Any

import pytest

from scripts.swarm_ledger.api import routes
from tests.swarm_ledger.test_api_v1 import authority_live as _authority_live
from tests.swarm_ledger.test_api_v1 import live as _live
from tests.swarm_ledger.test_api_v1 import request, stored

authority_live = _authority_live
live = _live
pytestmark = pytest.mark.unit


@pytest.mark.feature("swarm-orders-read")
def test_the_orders_read_serves_every_open_order(live: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    phases = [{"id": "p1", "title": "Build", "ordered": {"at": 7, "by": "operator"}}]
    tasks = [
        {"id": "t1", "lane": "eng", "state": "open", "phase": "p1"},
        {"id": "t2", "lane": "eng", "state": "done", "phase": "p1"},
    ]
    stored(monkeypatch, {"tasks": tasks, "phases": phases, "freezes": [], "_meta": {"rev": 1}})
    monkeypatch.setattr(routes, "fix_phase", lambda services, slug: "")
    answer: tuple[int, dict[str, Any]] = request(live, "GET", "orders")
    status, reply = answer
    assert status == 200 and reply["next_cursor"] is None and len(reply["revision"]) == 64
    [row] = reply["data"]
    assert (row["item"], row["by"], row["at"]) == ("phases/p1", "operator", 7)
    assert (row["claimable"], row["busy"], row["done"]) == (1, 0, ["t2"])
    assert row["tasks"] == [{"id": "t1", "lane": "eng", "state": "open", "verdict": "claimable"}]
