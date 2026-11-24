"""The ledger page's first reads answer from narrow reads, never from the whole ledger page view."""

import copy
from typing import Any, cast

import pytest

from scripts.swarm_ledger import ledger_closed, ledger_server
from scripts.swarm_ledger.api import resources, summary
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


@pytest.mark.feature("ledger-summary-read")
def test_the_summary_derives_what_the_page_view_derives() -> None:
    state = document()
    view = ledger_server.page_view(copy.deepcopy(state), "", False)
    parts = {key: value for key, value in state.items() if key != "tasks"}
    built = summary.build(parts, state["tasks"], ("", False), ledger_server.PAGE_LIMITS)
    assert built["marks"] == view["marks"]
    assert built["phase_status"] == view["phase_status"]
    hidden = cast(dict[str, dict[str, Any]], view["_meta"].get("closed", {}))
    counted = {name: {key: entry[key] for key in summary.CLOSED_KEPT if key in entry} for name, entry in hidden.items()}
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
    state["followups"] += [{"id": f"f-gone-{n}", "text": "Closed", "done": True} for n in range(5700)]
    parts = {key: value for key, value in state.items() if key != "tasks"}
    built = summary.build(parts, state["tasks"], ("", False), ledger_server.PAGE_LIMITS)
    assert resources.reply_size({"data": built, "revision": resources.revision(built)}) < resources.MAX_REPLY
    assert not {"stamps", "join_history", "master_decisions", "members"} & set(built["_meta"])
    assert len(built["_meta"]["events"]) == summary.EVENTS_KEPT
    assert {key for entry in built["_meta"]["closed"].values() for key in entry} <= set(summary.CLOSED_KEPT)
    assert built["_meta"]["closed"]["tasks"]["hidden"] == 5301 - ledger_closed.KEPT
