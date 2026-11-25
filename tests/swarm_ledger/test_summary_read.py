"""The ledger page's first reads answer from narrow reads, never from the whole ledger page view."""

import copy
import threading
import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from scripts.swarm_ledger import ledger_closed, ledger_server
from scripts.swarm_ledger.api import resources, routes, summary, task_list
from scripts.swarm_ledger.api.errors import APIError
from tests.swarm_ledger.test_api_v1 import authority_live, live, request, stored

__all__ = ["authority_live", "live"]


def document() -> dict:
    comment = {"id": "c-1", "by": "operator", "at": 5, "text": "Look here"}
    return {
        "title": "Report",
        "overview": "Intent",
        "sources": ["plan.md"],
        "priorities": [{"id": "pr-1", "item": "tasks/t-open", "text": "first"}],
        "plans": [],
        "slices": [{"id": "s-1", "phase": "p1", "title": "Slice"}],
        "freezes": [],
        "phases": [
            {"id": "p1", "title": "Build", "planning": "manual", "comments": [comment]},
            {"id": "p0", "title": "Old", "done": True},
        ],
        "tasks": [
            {"id": "t-done", "title": "Done", "state": "done", "done": True, "phase": "p1"},
            {"id": "t-open", "title": "Open", "state": "open", "phase": "p1", "rank": "high", "comments": [comment]},
            {"id": "t-claimed", "title": "Claimed", "state": "claimed", "phase": "p1", "claimed_by": "eng"},
            {"id": "t-pr", "title": "In review", "state": "pr", "phase": "p1"},
        ],
        "followups": [{"id": "f-1", "text": "Later"}, {"id": "f-2", "text": "Gone", "done": True}],
        "questions": [],
        "alerts": [],
        "notifications": [{"id": "n-1", "text": "Ping"}],
        "_meta": {
            "rev": 9,
            "updated_at": 200,
            "created_at": 10,
            "members": {"eng": {"role": "member", "joined_at": 1}},
            "events": [{"rev": 9, "kind": "sync requested", "by": "operator", "at": 190}],
        },
    }


def refuse_whole_view(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a first screen read built the whole ledger page view")

    for name in ("page_view", "ledger_view", "stream_resources"):
        monkeypatch.setattr(ledger_server, name, refuse)


@pytest.mark.feature("ledger-summary-read")
def test_the_summary_answers_without_building_the_whole_page_view(live: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    stored(monkeypatch, document())
    refuse_whole_view(monkeypatch)
    status, reply = request(live, "GET", "summary")
    assert status == 200
    data = reply["data"]
    assert reply["revision"] == resources.revision(data)
    assert (data["title"], data["overview"], data["sources"]) == ("Report", "Intent", ["plan.md"])
    assert data["_meta"]["rev"] == 9
    assert data["sections"]["tasks"] == {"open": 3, "states": {"open": 1, "claimed": 1, "pr": 1}}
    assert data["sections"]["phases"] == {"open": 1, "states": {"open": 1}}
    assert not set(summary.PAGED) & set(data)
    assert "members" not in data["_meta"]
    assert data["priorities"] == document()["priorities"]


@pytest.mark.feature("ledger-summary-read")
def test_an_open_first_page_answers_without_building_the_whole_page_view(
    live: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored(monkeypatch, document())
    refuse_whole_view(monkeypatch)
    status, reply = request(live, "GET", "tasks?state=open&offset=0&limit=2")
    assert status == 200
    assert [(row["id"], row["position"]) for row in reply["data"]] == [("t-done", 0), ("t-pr", 3)]
    assert (reply["total"], reply["next_offset"]) == (4, 2)
    status, reply = request(live, "GET", "tasks?state=open&offset=2&limit=2")
    assert [row["id"] for row in reply["data"]] == ["t-claimed", "t-open"]
    assert reply["data"][1]["comments_count"] == 1 and "comments" not in reply["data"][1]
    assert reply["next_offset"] is None
    status, reply = request(live, "GET", "phases?state=open&offset=0&limit=5")
    assert status == 200
    assert [row["id"] for row in reply["data"]] == ["p1", "p0"]
    status, reply = request(live, "GET", "followups?state=open&limit=5&q=x")
    assert status == 400
    assert reply == {"error": {"code": "schema_invalid", "message": routes.OPEN_ONLY}}
    status, reply = request(live, "GET", f"tasks?state=open&cursor={'0' * 64}:0")
    assert status == 400
    assert reply == {"error": {"code": "schema_invalid", "message": routes.OPEN_ONLY}}


@pytest.mark.feature("ledger-summary-read")
def test_a_summary_read_and_an_open_tasks_read_at_once_build_the_summary_once(
    live: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored(monkeypatch, document())
    monkeypatch.setattr(ledger_server, "SUMMARIES", {})
    build = summary.build
    builds: list[int] = []

    def slow(*args: Any) -> dict[str, Any]:
        builds.append(len(builds))
        time.sleep(0.3)
        return build(*args)

    monkeypatch.setattr(summary, "build", slow)
    answers: list[int] = []

    def read(path: str) -> None:
        answers.append(request(live, "GET", path)[0])

    readers = [threading.Thread(target=read, args=(path,)) for path in ("summary", "tasks?state=open&offset=0&limit=2")]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join()
    assert answers == [200, 200]
    assert builds == [0]


@pytest.mark.feature("ledger-summary-read")
def test_the_summary_derives_what_the_page_view_derives() -> None:
    state = document()
    view = ledger_server.page_view(copy.deepcopy(state), "", False)
    parts = {key: value for key, value in state.items() if key != "tasks"}
    built = summary.build(parts, state["tasks"], ("", False), ledger_server.PAGE_LIMITS)
    assert built["marks"] == view["marks"]
    assert built["phase_status"] == view["phase_status"]
    hidden = cast(dict[str, dict[str, Any]], view["_meta"].get("closed", {}))
    named = summary.referenced(state)
    counted = {name: summary.counted(entry, named) for name, entry in hidden.items()}
    assert built["_meta"]["closed"] == counted
    assert built["_meta"]["limits"] == view["_meta"]["limits"]


@pytest.mark.feature("ledger-summary-read")
def test_the_summary_of_a_production_sized_ledger_fits_one_reply() -> None:
    state = document()
    state["_meta"].update(
        stamps={f"tasks/t-{n}/state": {"at": n, "by": "eng", "rev": n, "op": "update"} for n in range(60000)},
        join_history=[{"by": f"agent-{n}", "at": n, "role": "member"} for n in range(6000)],
        master_decisions=[{"at": n, "text": "Decided " * 8} for n in range(4000)],
        events=[
            {"rev": n, "kind": "sync requested", "by": "operator", "at": n, "text": "x" * 120} for n in range(4700)
        ],
    )
    state["tasks"] += [
        {"id": f"t-gone-{n}", "title": "Gone", "state": "done", "done": True, "claimed_by": "eng"} for n in range(5300)
    ]
    state["tasks"].append({"id": "t-waits", "title": "Waits", "state": "open", "depends_on": ["t-gone-0"]})
    state["followups"] += [{"id": f"f-gone-{n}", "text": "Closed", "done": True} for n in range(5700)]
    parts = {key: value for key, value in state.items() if key != "tasks"}
    built = summary.build(parts, state["tasks"], ("", False), ledger_server.PAGE_LIMITS)
    assert resources.reply_size({"data": built, "revision": resources.revision(built)}) < resources.MAX_REPLY
    assert not {"stamps", "join_history", "master_decisions", "members"} & set(built["_meta"])
    assert len(built["_meta"]["events"]) == summary.EVENTS_KEPT
    assert {key for entry in built["_meta"]["closed"].values() for key in entry} <= {*summary.CLOSED_KEPT, "ids"}
    assert built["_meta"]["closed"]["tasks"]["ids"] == ["t-gone-0"]
    assert built["_meta"]["closed"]["followups"]["ids"] == []
    assert built["_meta"]["closed"]["tasks"]["hidden"] == 5301 - ledger_closed.KEPT


@pytest.mark.feature("ledger-summary-read")
def test_the_summary_crew_holds_only_members_seen_in_the_active_window() -> None:
    now = 10 * ledger_server.CREW_WINDOW_MS
    members = {
        f"agent-{n}": {"role": "member", "last_seen": now - ledger_server.CREW_WINDOW_MS - 1} for n in range(5600)
    }
    members["agent-here"] = {"role": "member", "last_seen": now - 5}
    crew = ledger_server.active_crew({"members": members}, now)
    assert [row["last_seen"] for row in crew] == [now - 5]


@pytest.mark.feature("ledger-summary-read")
def test_build_keeps_the_trimmed_view_of_a_collection_outside_paged_sections() -> None:
    state = document()
    state["questions"] = [
        {"id": f"q-{n}", "text": "Q", "answers": [{"by": "operator", "text": "A"}]} for n in range(11)
    ]
    parts = {key: value for key, value in state.items() if key != "tasks"}
    built = summary.build(parts, state["tasks"], ("", False), {})
    assert built["questions"] == state["questions"][1:]


@pytest.mark.feature("ledger-summary-read")
def test_referenced_names_task_dependencies_parked_tasks_and_priority_targets() -> None:
    state = {
        "tasks": [
            {"id": "t-1", "depends_on": ["t-dep"], "unrelated": 1},
            {"id": "t-2", "parked_on": ["t-held"]},
        ],
        "priorities": [{"id": "pr-1", "item": "tasks/abc/def"}],
    }
    assert summary.referenced(state) == {"t-dep", "t-held", "abc/def"}


@pytest.mark.feature("ledger-summary-read")
def test_page_order_sorts_unheld_open_rows_by_their_actual_rank() -> None:
    rows = [
        (0, {"id": "low", "rank": "low"}),
        (1, {"id": "urgent", "rank": "urgent"}),
        (2, {"id": "normal"}),
    ]
    ordered = summary.page_order(rows, frozen=set())
    assert [row["id"] for _, row in ordered] == ["urgent", "normal", "low"]


@pytest.mark.feature("ledger-summary-read")
def test_page_order_defaults_a_missing_state_to_open_and_an_unknown_state_last() -> None:
    rows = [
        (0, {"id": "no-state"}),
        (1, {"id": "blocked", "state": "blocked"}),
        (2, {"id": "weird", "state": "totally-unknown"}),
    ]
    ordered = summary.page_order(rows, frozen=set())
    assert [row["id"] for _, row in ordered] == ["no-state", "blocked", "weird"]


@pytest.mark.feature("ledger-summary-read")
def test_page_order_holds_only_the_rows_whose_task_id_is_frozen() -> None:
    rows = [
        (0, {"id": "t-y"}),
        (1, {"id": "t-x"}),
    ]
    ordered = summary.page_order(rows, frozen={"tasks/t-y"})
    assert [row["id"] for _, row in ordered] == ["t-x", "t-y"]


@pytest.mark.feature("ledger-summary-read")
def test_view_rows_drops_closed_rows_beyond_the_kept_window() -> None:
    rows = [{"id": f"c-{n}", "done": True} for n in range(12)] + [{"id": "open-1"}]
    kept = summary.view_rows("phases", rows, set())
    assert [row["id"] for _, row in kept] == [f"c-{n}" for n in range(2, 12)] + ["open-1"]


class _FakeTaskRepository:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def task_rows(self, slug: str, cached: object) -> tuple[None, dict[str, int], list[Any]]:
        self.calls.append(slug)
        return (None, {"rev": 1}, [])


@pytest.mark.feature("ledger-summary-read")
def test_rows_holds_its_cached_tasks_object_under_the_ledger_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_list, "HELD", {})
    repository = _FakeTaskRepository()
    task_list.rows(repository, "slug-a")
    assert set(task_list.HELD) == {"slug-a"}


@pytest.mark.feature("ledger-summary-read")
@pytest.mark.parametrize(
    ("path", "closed_row"),
    [
        ("tasks", {"id": "closed", "state": "done", "done": True}),
        ("questions", {"id": "closed", "answers": [{"text": "Answered"}]}),
    ],
)
def test_done_reads_select_only_the_requested_repository_rows(path: str, closed_row: dict[str, Any]) -> None:
    repository = Mock()
    rows = [{"id": "open"}, closed_row]
    repository.task_rows.return_value = (None, {"rev": 7}, rows)
    repository.read.return_value = {path: rows}
    repository.current.side_effect = AssertionError("A closed read must not load the whole ledger")
    services = cast(Any, SimpleNamespace(repository=repository))
    query = {"state": "done"}
    reply = routes.ledger_read(services, "selected-ledger", path, query)
    assert reply == resources.closed_page({path: rows}, path, query)
    assert [row["id"] for row in reply["data"]] == ["closed"]
    repository.current.assert_not_called()
    if path == "tasks":
        repository.task_rows.assert_called_once_with("selected-ledger", None)
        repository.read.assert_not_called()
    else:
        repository.read.assert_called_once_with("selected-ledger", path)
        repository.task_rows.assert_not_called()


@pytest.mark.feature("ledger-summary-read")
def test_a_cursor_read_uses_only_the_requested_repository_record() -> None:
    doc = {"tasks": [{"id": "t1", "comments": [{"id": "first", "text": "One"}, {"id": "second", "text": "Two"}]}]}
    path = "tasks/t1/comments"
    first = resources.read(doc, path, {"limit": 1})
    query = {"limit": 1, "cursor": first["next_cursor"]}
    repository = Mock()
    repository.read.return_value = doc
    repository.current.side_effect = AssertionError("A cursor read must not load the whole ledger")
    services = cast(Any, SimpleNamespace(repository=repository))
    reply = routes.ledger_read(services, "selected-ledger", path, query)
    assert reply == resources.read(doc, path, query)
    assert [row["id"] for row in reply["data"]] == ["second"]
    assert reply["next_cursor"] is None
    repository.read.assert_called_once_with("selected-ledger", "tasks/t1")
    repository.current.assert_not_called()


@pytest.mark.feature("ledger-summary-read")
def test_summary_sections_apply_collection_specific_closure() -> None:
    assert summary.section("questions", [{"id": "answered", "answers": [{"text": "Yes"}]}, {"id": "unanswered"}]) == {
        "open": 1,
        "states": {"open": 1},
    }
    assert summary.section("alerts", [{"id": "open", "done": True}, {"id": "closed", "state": "done"}]) == {
        "open": 1,
        "states": {"open": 1},
    }


@pytest.mark.feature("ledger-summary-read")
def test_summary_sections_count_repeated_states_and_exclude_deleted_rows() -> None:
    rows = [
        {"id": "first", "state": "open"},
        {"id": "second", "state": "open"},
        {"id": "missing-state"},
        {"id": "outside", "out_of_scope": True},
        {"id": "deleted", "deleted": True},
        {"id": "done", "done": True},
    ]
    assert summary.section("tasks", rows) == {"open": 4, "states": {"open": 3, "out": 1}}


@pytest.mark.feature("ledger-summary-read")
@pytest.mark.parametrize("query", ["q=x", f"cursor={'0' * 64}:0"])
def test_the_open_route_guard_refuses_query_and_cursor_keys_directly(query: str) -> None:
    handler = SimpleNamespace(path=f"/api/v1/ledgers/selected-ledger/tasks?state=open&{query}", headers={})
    with pytest.raises(APIError) as caught:
        routes.ledger_get(handler, cast(Any, SimpleNamespace()), "selected-ledger", "tasks")
    assert caught.value.status == 400
    assert caught.value.envelope() == {"error": {"code": "schema_invalid", "message": routes.OPEN_ONLY}}
