import pytest

from scripts.swarm_ledger.api import resources, routes
from scripts.swarm_ledger.api.errors import APIError

pytestmark = pytest.mark.unit


def doc():
    tasks = [{"id": f"t{n}", "title": f"Task {n}", "state": "done", "done": True} for n in range(25)]
    tasks[4]["title"] = "Rotate the Quota Router"
    questions = [{"id": "q1", "text": "Which quota window?", "answers": [{"id": "a1", "text": "five hours"}]}]
    return {"tasks": [*tasks, {"id": "open", "title": "Quota open", "state": "open"}], "questions": questions}


def ids(reply):
    return [row["id"] for row in reply["data"]]


def test_a_search_pages_only_the_done_rows_whose_title_matches_ignoring_case():
    reply = resources.read(doc(), "tasks", {"state": "done", "q": "quota ROUTER"})
    assert ids(reply) == ["t4"]
    assert reply["total"] == 1
    assert reply["next_offset"] is None


def test_a_search_matches_the_id_and_pages_from_the_offset_newest_first():
    reply = resources.read(doc(), "tasks", {"state": "done", "q": "T1", "offset": 0, "limit": 5})
    assert ids(reply) == ["t19", "t18", "t17", "t16", "t15"]
    assert reply["total"] == 11
    assert reply["next_offset"] == 5
    rest = resources.read(doc(), "tasks", {"state": "done", "q": "T1", "offset": 10})
    assert ids(rest) == ["t1"]
    assert rest["next_offset"] is None


def test_a_search_matches_question_text():
    assert ids(resources.read(doc(), "questions", {"state": "done", "q": "window"})) == ["q1"]
    assert ids(resources.read(doc(), "questions", {"state": "done", "q": "router"})) == []


def test_an_empty_search_reads_every_done_row():
    assert resources.read(doc(), "tasks", {"state": "done", "q": ""})["total"] == 25


def test_a_search_without_the_done_state_is_refused():
    with pytest.raises(APIError) as caught:
        resources.read(doc(), "tasks", {"q": "quota"})
    assert caught.value.status == 400
    assert caught.value.envelope() == {
        "error": {"code": "schema_invalid", "message": "State and offset read the done rows of a collection"}
    }


def test_the_query_string_carries_the_search_text():
    assert routes.pagination("/api/v1/ledgers/x/tasks?state=done&q=quota%20router&offset=0&limit=20") == {
        "state": "done",
        "q": "quota router",
        "offset": 0,
        "limit": 20,
    }


def test_a_search_longer_than_two_hundred_characters_is_refused():
    routes.pagination("/x?state=done&q=" + "a" * 200)
    with pytest.raises(APIError) as caught:
        routes.pagination("/x?state=done&q=" + "a" * 201)
    assert caught.value.status == 400
