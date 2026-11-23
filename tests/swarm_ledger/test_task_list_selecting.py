"""The ledger API's narrow tasks selection: rows by ids, states, a hive or workstation pin and a branch with a pull
request, every given filter matched, optionally projected to named fields, and the client query that asks for it."""

from typing import Any
from urllib.parse import urlencode

import pytest

from scripts.swarm import ledger_client
from scripts.swarm_ledger.api import task_list

ROWS: list[dict[str, Any]] = [
    {"id": "a", "state": "open", "title": "A", "comments": [{"text": "x"}]},
    {"id": "b", "state": "claimed", "title": "B", "hive": "root"},
    {"id": "c", "state": "pr", "title": "C", "branch": "c-branch", "pr_url": "https://example.invalid/pull/3"},
    {"id": "d", "state": "open", "title": "D", "workstation": True},
    {"id": "e", "state": "done", "title": "E", "branch": "e-branch"},
]


def ids(reply: dict[str, Any]) -> list[str]:
    return [row["id"] for row in reply["data"]]


@pytest.mark.feature("hub-admission-narrow-read")
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ({"ids": "a,c,missing"}, ["a", "c"]),
        ({"states": "claimed,pr"}, ["b", "c"]),
        ({"states": "open", "pinned": "1"}, ["d"]),
        ({"pinned": "1"}, ["b", "d"]),
        ({"branches": "c-branch,e-branch,gone"}, ["c", "e"]),
        ({"ids": "a,b", "states": "claimed"}, ["b"]),
    ],
    ids=["ids", "states", "open-pinned", "pinned", "branches", "ids-and-states"],
)
def test_a_selection_returns_the_rows_matching_every_filter_it_names(
    query: dict[str, str], expected: list[str]
) -> None:
    assert ids(task_list.selecting(ROWS, query)) == expected


@pytest.mark.feature("hub-admission-narrow-read")
def test_a_selection_projects_rows_without_threads_and_keeps_only_the_named_fields() -> None:
    whole = task_list.selecting(ROWS, {"ids": "a"})["data"][0]
    assert "comments" not in whole and whole["comments_count"] == 1 and whole["title"] == "A"

    narrow = task_list.selecting(ROWS, {"branches": "c-branch,e-branch", "fields": "branch,pr_url"})["data"]
    assert narrow == [
        {"id": "c", "branch": "c-branch", "pr_url": "https://example.invalid/pull/3"},
        {"id": "e", "branch": "e-branch"},
    ]


@pytest.mark.feature("hub-admission-narrow-read")
def test_the_client_query_names_only_the_filters_given() -> None:
    assert ledger_client.where_query(ids=["a", "b"]) == {"ids": "a,b"}
    assert ledger_client.where_query(states=["open"], pinned=True) == {"states": "open", "pinned": "1"}
    assert ledger_client.where_query(branches=["x", "y"], fields=["branch", "pr_url"]) == {
        "branches": "x,y",
        "fields": "branch,pr_url",
    }
    assert ledger_client.where_query() == {}


@pytest.mark.feature("hub-admission-narrow-read")
@pytest.mark.parametrize(
    ("path", "query", "selected"),
    [
        ("tasks", {"ids": "a"}, True),
        ("tasks", {"branches": "x", "fields": "branch"}, True),
        ("tasks", {"fields": "branch"}, False),
        ("tasks", {"ids": ""}, False),
        ("tasks", {"ids": "a", "cursor": "c"}, False),
        ("phases", {"ids": "a"}, False),
    ],
    ids=["ids", "branches-fields", "fields-only", "empty-ids", "with-cursor", "not-tasks"],
)
def test_only_a_plain_tasks_read_naming_a_filter_is_a_selection(
    path: str, query: dict[str, str], selected: bool
) -> None:
    assert task_list.selection(path, query) is selected


@pytest.mark.feature("hub-admission-narrow-read")
@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"ids": ("t2", "gone")}, ["t2"]),
        ({"states": ("open",)}, []),
        ({"pinned": True}, []),
        ({"branches": ("c-branch",)}, []),
        ({"ids": ("t1", "t2"), "fields": ("state",)}, ["t1", "t2"]),
    ],
    ids=["ids", "states", "pinned", "branches", "fields"],
)
def test_the_tasks_route_serves_every_selection_the_client_asks_for(
    filters: dict[str, Any], expected: list[str]
) -> None:
    from tests.swarm_ledger.test_api_services import SLUG, fake_services, served

    query = urlencode(ledger_client.where_query(**filters))
    status, body = served(fake_services([]), "GET", f"/api/v1/ledgers/{SLUG}/tasks?{query}")

    assert (status, ids(body) if status == 200 else body) == (200, expected)
