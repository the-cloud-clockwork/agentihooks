import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm.health import focus_check, order_check
from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.api import routes
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.repository import sqlite as store
from tests.swarm.test_freeze import PLANS, doc, record, task
from tests.swarm_ledger.api_fakes import as_services

pytestmark = pytest.mark.unit

SLUG = "orders-read"
MASTER = "master@c0ffee-0001"
ORDER = {"at": 5, "by": MASTER, "quote": "Build the reviewer seats"}


def phases(**marks: Any) -> list[dict[str, Any]]:
    rows = [{"id": "p1", "title": "Build", "plan": "plans/a"}, {"id": "p2", "title": "Ship", "plan": "plans/b"}]
    return [{**row, **({"ordered": marks[row["id"]]} if row["id"] in marks else {})} for row in rows]


def ledger_doc(tasks: list[dict[str, Any]], *records: dict[str, Any], **marks: Any) -> dict[str, Any]:
    return {**doc(*records, tasks=tasks), "phases": phases(**marks)}


@pytest.mark.feature("swarm-orders-read")
def test_an_ordered_phase_lists_every_task_under_it_with_its_verdict_and_counts() -> None:
    tasks = [
        task("t1"),
        task("t2", depends_on=["t9"]),
        task("t3", state="claimed", claimed_by="engineer@one"),
        task("t4", state="pr"),
        task("t5", state="done"),
        task("t9", phase="p2"),
    ]
    [row] = order_check.read(ledger_doc(tasks, p1=ORDER), "")
    assert row == {
        "item": "phases/p1",
        "title": "Build",
        "by": MASTER,
        "at": 5,
        "quote": "Build the reviewer seats",
        "claimable": 1,
        "busy": 2,
        "done": ["t5"],
        "tasks": [
            {"id": "t1", "lane": "eng", "state": "open", "verdict": "claimable"},
            {"id": "t2", "lane": "eng", "state": "open", "verdict": "it waits on task t9"},
            {"id": "t3", "lane": "eng", "state": "claimed", "verdict": "it is claimed by engineer@one"},
            {"id": "t4", "lane": "eng", "state": "pr", "verdict": "it is in a pull request"},
        ],
        "line": "phases/p1 Build: 4 unfinished, 1 claimable, 2 claimed or in a pull request, 1 done",
    }


@pytest.mark.feature("swarm-orders-read")
def test_an_ordered_task_lists_itself_and_an_order_with_no_task_lists_none() -> None:
    mark = {"at": 9, "by": "operator"}
    found = order_check.read(ledger_doc([task("t1", ordered=mark, title="Fix the writer"), task("t2")], p2=ORDER), "")
    assert found == [
        {
            "item": "phases/p2",
            "title": "Ship",
            "by": MASTER,
            "at": 5,
            "quote": "Build the reviewer seats",
            "claimable": 0,
            "busy": 0,
            "done": [],
            "tasks": [],
            "line": "phases/p2 Ship: 0 unfinished, 0 claimable, 0 claimed or in a pull request, 0 done",
        },
        {
            "item": "tasks/t1",
            "title": "Fix the writer",
            "by": "operator",
            "at": 9,
            "claimable": 1,
            "busy": 0,
            "done": [],
            "tasks": [{"id": "t1", "lane": "eng", "state": "open", "verdict": "claimable"}],
            "line": "tasks/t1 Fix the writer: 1 unfinished, 1 claimable, 0 claimed or in a pull request, 0 done",
        },
    ]


@pytest.mark.feature("swarm-orders-read")
def test_orders_read_oldest_first_and_an_entry_he_wrote_reads_as_the_request() -> None:
    asked = {"at": 3, "by": MASTER, "request": "c1"}
    found = order_check.read(ledger_doc([task("t1", ordered=asked), task("t2", ordered=ORDER)], p1=ORDER, p2=asked), "")
    assert [row["item"] for row in found] == ["phases/p2", "tasks/t1", "phases/p1", "tasks/t2"]
    assert (found[0]["request"], "quote" in found[0]) == ("c1", False)
    assert found[1]["line"] == "tasks/t1: 1 unfinished, 1 claimable, 0 claimed or in a pull request, 0 done"


@pytest.mark.feature("swarm-orders-read")
def test_a_done_or_out_of_scope_order_and_a_malformed_mark_are_left_out() -> None:
    tasks = [
        task("t1", ordered=ORDER, state="done"),
        task("t2", ordered=ORDER, done=True),
        task("t3", ordered=ORDER, out_of_scope=True),
        task("t4", ordered="yes"),
        task("t5", ordered={"by": MASTER}),
    ]
    ledger_state = ledger_doc(tasks)
    ledger_state["phases"] = [
        {"id": "p1", "title": "Build", "plan": "plans/a", "ordered": ORDER, "done": True},
        {"id": "p2", "title": "Ship", "plan": "plans/b", "ordered": ORDER, "out_of_scope": True},
    ]
    assert order_check.read(ledger_state, "") == []
    assert order_check.read({"plans": PLANS}, "") == []


@pytest.mark.feature("swarm-orders-read")
def test_an_out_of_scope_task_under_an_order_is_not_counted_as_unfinished_work() -> None:
    tasks = [
        task("t1"),
        task("t2", out_of_scope=True),
        task("t3", state="claimed", out_of_scope=True),
        task("t4", done=True),
    ]
    [row] = order_check.read(ledger_doc(tasks, p1=ORDER), "")
    assert row["tasks"] == [{"id": "t1", "lane": "eng", "state": "open", "verdict": "claimable"}]
    assert (row["claimable"], row["busy"], row["done"]) == (1, 0, ["t4"])
    assert row["line"] == "phases/p1 Build: 1 unfinished, 1 claimable, 0 claimed or in a pull request, 1 done"
    [alone] = order_check.read(ledger_doc([task("t2", out_of_scope=True)], p1=ORDER), "")
    assert (alone["tasks"], alone["line"]) == (
        [],
        "phases/p1 Build: 0 unfinished, 0 claimable, 0 claimed or in a pull request, 0 done",
    )


@pytest.mark.feature("swarm-orders-read")
def test_a_task_whose_phase_waits_for_its_planner_reads_that_reason() -> None:
    ledger_state = ledger_doc([task("t1")], p1=ORDER)
    ledger_state["phases"][0]["planning"] = "auto"
    [row] = order_check.read(ledger_state, "")
    assert row["tasks"] == [
        {"id": "t1", "lane": "eng", "state": "open", "verdict": "its phase Build is waiting for its planner"}
    ]
    assert row["claimable"] == 0


@pytest.mark.feature("swarm-orders-read")
def test_each_task_under_an_order_reads_the_reason_the_claim_gate_holds_it() -> None:
    tasks = [task("t1"), task("t2"), task("t3", depends_on=["t1"]), task("t9", phase="p2")]
    ledger_state = ledger_doc(tasks, record("tasks/t9", "focus"), record("tasks/t2"), p1=ORDER)
    [row] = order_check.read(ledger_state, "")
    seen = focus_check.view(ledger_state, "")
    verdicts = {entry["id"]: entry["verdict"] for entry in row["tasks"]}
    assert verdicts == {
        "t1": "a focus elsewhere holds it",
        "t2": "the freeze on task t2 holds it",
        "t3": "a focus elsewhere holds it",
    }
    assert verdicts == {entry["id"]: focus_check.verdict(entry, seen) for entry in tasks[:3]}
    assert (row["claimable"], row["busy"]) == (0, 0)
    [fixed] = order_check.read(ledger_state, "p1")
    assert {entry["id"]: entry["verdict"] for entry in fixed["tasks"]} == {
        "t1": "claimable",
        "t2": "the freeze on task t2 holds it",
        "t3": "it waits on task t1",
    }
    assert fixed["claimable"] == 1


def served(tmp_path: Path, tasks: list[dict[str, Any]], *records: dict[str, Any]) -> Any:
    repo = store.SQLiteLedgerRepository(tmp_path / store.DATABASE)
    repo.import_document(SLUG, {**ledger_doc(tasks, *records, p1=ORDER), "_meta": {"rev": 1}})
    return as_services(SimpleNamespace(repository=repo))


@pytest.fixture
def reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, bool]]:
    services = served(tmp_path, [task("t1")])
    seen: list[tuple[str, str, bool]] = []

    def resource(slug: str, path: str, collection: bool = False) -> Any:
        seen.append((slug, path, collection))
        return routes.ledger_read(services, slug, path, {})["data"]

    monkeypatch.setattr(routes, "fix_phase", lambda services, slug: "")
    monkeypatch.setattr(ledger, "resource", resource)
    return seen


@pytest.mark.feature("swarm-orders-read")
def test_ledger_get_orders_prints_every_open_order_through_the_ledger_api(
    reads: list[tuple[str, str, bool]], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert ledger._ledger_item("orders") == "orders"
    monkeypatch.setattr(sys, "argv", ["ledger", "--slug", SLUG, "get", "orders"])
    ledger.main()
    [row] = json.loads(capsys.readouterr().out)
    assert row["line"] == "phases/p1 Build: 1 unfinished, 1 claimable, 0 claimed or in a pull request, 0 done"
    assert row["tasks"] == [{"id": "t1", "lane": "eng", "state": "open", "verdict": "claimable"}]
    assert reads == [(SLUG, "orders", True)]


@pytest.mark.feature("swarm-orders-read")
def test_an_orders_read_past_the_reply_limit_names_the_read_that_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = [task(f"t{number}") for number in range(40)]
    services = served(tmp_path, tasks)
    monkeypatch.setattr(routes, "fix_phase", lambda services, slug: "")
    whole = routes.ledger_read(services, SLUG, "orders", {})
    size = routes.resources.reply_size(whole)
    assert size > 1000
    with monkeypatch.context() as limited:
        limited.setattr(routes.resources, "MAX_REPLY", size)
        assert routes.ledger_read(services, SLUG, "orders", {}) == whole
        limited.setattr(routes.resources, "MAX_REPLY", size - 1)
        with pytest.raises(APIError) as refused:
            routes.ledger_read(services, SLUG, "orders", {})
    message = "The open orders pass the reply limit: read the tasks of one order with focus/<item>"
    assert (refused.value.status, refused.value.code, str(refused.value)) == (413, "resource_too_large", message)
    one = routes.ledger_read(services, SLUG, "focus/phases/p1", {})
    assert [row["id"] for row in one["data"]["tasks"]] == [row["id"] for row in tasks]


@pytest.mark.feature("swarm-orders-read")
def test_the_orders_route_reads_with_the_fix_phase_of_its_own_swarm_and_revises_its_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = served(tmp_path, [task("t1"), task("t9", phase="p2")], record("phases/p2", "focus"))
    asked: list[tuple[Any, str]] = []

    def fix_phase(asking: Any, slug: str) -> str:
        asked.append((asking, slug))
        return "p1"

    monkeypatch.setattr(routes, "fix_phase", fix_phase)
    reply = routes.ledger_read(services, SLUG, "orders", {})
    assert asked == [(services, SLUG)]
    [row] = reply["data"]
    assert (row["item"], row["claimable"], row["tasks"][0]["verdict"]) == ("phases/p1", 1, focus_check.CLAIMABLE)
    assert reply == {"data": [row], "revision": routes.resources.revision([row]), "next_cursor": None}
    monkeypatch.setattr(routes, "fix_phase", lambda asking, slug: "")
    [held] = routes.ledger_read(services, SLUG, "orders", {})["data"]
    assert (held["claimable"], held["tasks"][0]["verdict"]) == (0, "a focus elsewhere holds it")


@pytest.mark.feature("swarm-orders-read")
def test_an_order_mark_that_names_no_author_and_no_proof_reads_with_an_empty_author() -> None:
    [row] = order_check.read(ledger_doc([task("t1")], p1={"at": 5}), "")
    assert (row["by"], row["at"], "quote" in row, "request" in row) == ("", 5, False, False)
