"""The ledger API's narrow tasks selection: rows by ids, states, a hive or workstation pin and a branch with a pull
request, every given filter matched, optionally projected to named fields, and the client query that asks for it."""

from typing import Any

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
        ({"branched": "1"}, ["c"]),
        ({"ids": "a,b", "states": "claimed"}, ["b"]),
    ],
    ids=["ids", "states", "open-pinned", "pinned", "branched", "ids-and-states"],
)
def test_a_selection_returns_the_rows_matching_every_filter_it_names(
    query: dict[str, str], expected: list[str]
) -> None:
    assert ids(task_list.selecting(ROWS, query)) == expected


@pytest.mark.feature("hub-admission-narrow-read")
def test_a_selection_projects_rows_without_threads_and_keeps_only_the_named_fields() -> None:
    whole = task_list.selecting(ROWS, {"ids": "a"})["data"][0]
    assert "comments" not in whole and whole["comments_count"] == 1 and whole["title"] == "A"

    narrow = task_list.selecting(ROWS, {"branched": "1", "fields": "branch,pr_url"})["data"]
    assert narrow == [{"id": "c", "branch": "c-branch", "pr_url": "https://example.invalid/pull/3"}]


@pytest.mark.feature("hub-admission-narrow-read")
def test_the_client_query_names_only_the_filters_given() -> None:
    assert ledger_client.where_query(ids=["a", "b"]) == {"ids": "a,b"}
    assert ledger_client.where_query(states=["open"], pinned=True) == {"states": "open", "pinned": "1"}
    assert ledger_client.where_query(branched=True, fields=["branch", "pr_url"]) == {
        "branched": "1",
        "fields": "branch,pr_url",
    }
