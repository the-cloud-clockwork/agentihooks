from types import SimpleNamespace

import pytest

from scripts.swarm_ledger.api import resources, routes
from scripts.swarm_ledger.api.errors import APIError

pytestmark = pytest.mark.unit

COMMENT = {"id": "c1", "by": "agent", "at": 1, "text": "landed"}
REFUSED = {"error": {"code": "schema_invalid", "message": "State and offset read the done rows of a collection"}}


def doc(count=35):
    tasks = [
        {"id": f"t{n}", "title": f"Task {n}", "state": "done", "done": True, "comments": [COMMENT]}
        for n in range(count)
    ]
    return {"tasks": [*tasks, {"id": "open", "title": "Open", "state": "open", "done": False}], "_meta": {"rev": 3}}


def ids(reply):
    return [row["id"] for row in reply["data"]]


def test_done_rows_page_newest_first_from_the_offset_with_their_threads():
    reply = resources.read(doc(), "tasks", {"state": "done", "offset": 10, "limit": 10})
    assert ids(reply) == [f"t{n}" for n in range(24, 14, -1)]
    assert reply["data"][0]["comments"] == [COMMENT]
    assert reply["total"] == 35
    assert reply["next_offset"] == 20
    assert reply["revision"] == resources.revision(reply["data"])


def test_the_last_done_page_has_no_next_offset():
    reply = resources.read(doc(), "tasks", {"state": "done", "offset": 30})
    assert ids(reply) == [f"t{n}" for n in range(4, -1, -1)]
    assert reply["next_offset"] is None
    assert reply["total"] == 35


def test_the_done_page_defaults_to_ten_rows_from_the_newest():
    reply = resources.read(doc(), "tasks", {"state": "done"})
    assert ids(reply) == [f"t{n}" for n in range(34, 24, -1)]
    assert reply["next_offset"] == 10


def test_an_offset_past_the_end_reads_an_empty_page():
    reply = resources.read(doc(3), "tasks", {"state": "done", "offset": 9})
    assert reply == {"data": [], "revision": resources.revision([]), "total": 3, "next_offset": None}


def test_a_done_page_takes_rows_while_the_whole_reply_fits_the_bound(monkeypatch):
    big = doc(12)
    for row in big["tasks"]:
        row["description"] = "x" * 1000
    newest = list(reversed(big["tasks"][:12]))
    monkeypatch.setattr(resources, "MAX_REPLY", resources.reply_size(resources.closed_reply(newest[:2], 12, 0)))
    reply = resources.read(big, "tasks", {"state": "done", "offset": 0, "limit": 10})
    assert ids(reply) == ["t11", "t10"]
    assert reply == resources.closed_reply(newest[:2], 12, 0)
    assert reply["next_offset"] == 2


def test_a_done_row_larger_than_the_bound_is_refused(monkeypatch):
    monkeypatch.setattr(resources, "MAX_REPLY", 300)
    big = doc(2)
    big["tasks"][1]["description"] = "x" * 1000
    with pytest.raises(APIError) as caught:
        resources.read(big, "tasks", {"state": "done"})
    assert caught.value.status == 413
    assert caught.value.envelope() == {
        "error": {"code": "resource_too_large", "message": "Use the explicit export operation for this resource"}
    }


@pytest.mark.parametrize("path", ["chat", "notes", "tasks/t1", "metadata"])
def test_state_or_offset_on_a_resource_without_done_rows_is_refused(path):
    for query in ({"state": "done"}, {"offset": 3}):
        with pytest.raises(APIError) as caught:
            resources.read({**doc(), "chat": [], "notes": []}, path, query)
        assert caught.value.status == 400
        assert caught.value.envelope() == REFUSED


def test_an_offset_without_the_done_state_is_refused_on_a_collection():
    with pytest.raises(APIError) as caught:
        resources.read(doc(), "tasks", {"offset": 3})
    assert caught.value.status == 400
    assert caught.value.envelope() == REFUSED


@pytest.mark.parametrize("query", [{"state": "done"}, {"offset": 0}])
def test_a_plain_page_refuses_state_or_offset(query):
    with pytest.raises(APIError) as caught:
        resources.page([{"id": "a"}], resources.revision([]), query)
    assert caught.value.status == 400
    assert caught.value.envelope() == REFUSED


def test_a_done_page_honours_a_smaller_limit():
    reply = resources.read(doc(), "tasks", {"state": "done", "offset": 10, "limit": 3})
    assert ids(reply) == ["t24", "t23", "t22"]
    assert reply["next_offset"] == 13


def test_answered_questions_page_by_their_answers_not_a_done_flag():
    questions = [
        {"id": "q1", "text": "Old", "answers": [{"id": "a", "text": "yes"}]},
        {"id": "q2", "text": "Open", "answers": []},
        {"id": "q3", "text": "New", "answers": [{"id": "b", "text": "no"}]},
    ]
    reply = resources.read({"questions": questions, "_meta": {"rev": 1}}, "questions", {"state": "done"})
    assert ids(reply) == ["q3", "q1"]
    assert reply["total"] == 2


def test_the_query_string_carries_the_state_and_an_integer_offset():
    assert routes.pagination("/api/v1/ledgers/s/tasks?state=done&offset=20&limit=10") == {
        "state": "done",
        "offset": 20,
        "limit": 10,
    }


@pytest.mark.parametrize("query", ["state=open", "offset=-1", "offset=x", "offset=1000001"])
def test_the_query_string_refuses_another_state_or_a_bad_offset(query):
    with pytest.raises(APIError) as caught:
        routes.pagination(f"/api/v1/ledgers/s/tasks?{query}")
    assert caught.value.status == 400


@pytest.mark.parametrize("path", ["architecture", "controller-lease", "hierarchy", "swarm", "swarm/agents", "events"])
def test_the_route_refuses_state_or_offset_before_any_other_read(path):
    for query in ("state=done", "offset=3", "q=router"):
        handler = SimpleNamespace(path=f"/api/v1/ledgers/s/{path}?{query}", headers={})
        with pytest.raises(APIError) as caught:
            routes.ledger_get(handler, None, "s", path)
        assert caught.value.status == 400
        assert caught.value.envelope() == REFUSED
