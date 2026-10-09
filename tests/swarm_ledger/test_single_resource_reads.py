import pytest

from scripts.swarm_ledger.api import resources
from scripts.swarm_ledger.repository import repository, sqlite
from tests.swarm_ledger.test_api_v1 import authority_live as _authority_live
from tests.swarm_ledger.test_api_v1 import live as _live
from tests.swarm_ledger.test_api_v1 import request
from tests.swarm_ledger.test_ledger_authority import SLUG, core

pytestmark = pytest.mark.xdist_group("fakeredis")
authority_live = _authority_live
live = _live


def seed():
    core.sync(
        SLUG,
        ops=[
            {"op": "task_add", "id": "seed-task", "by": "swarm", "task": "t1", "title": "Seed", "lane": "eng"},
            {"op": "add", "id": "seed-comment", "thread": "tasks/t1/comments", "text": "Task note"},
            {"op": "add_item", "id": "q1", "by": "boss", "list": "questions", "text": "Question"},
            {"op": "add_item", "id": "f1", "by": "boss", "list": "followups", "text": "Follow up"},
        ],
    )
    state = repository.get_document(SLUG)
    return [
        "tasks/t1",
        "tasks/t1/comments",
        f"phases/{state['phases'][0]['id']}",
        f"questions/{state['questions'][0]['id']}",
        f"questions/{state['questions'][0]['id']}/answers",
        f"followups/{state['followups'][0]['id']}",
    ]


@pytest.fixture
def whole_loads(monkeypatch):
    loads = []
    get_document, read_rows = sqlite.SQLiteLedgerRepository.get_document, sqlite.read_rows

    def counted_document(self, slug):
        loads.append(("document", slug))
        return get_document(self, slug)

    def counted_rows(connection, slug):
        loads.append(("rows", slug))
        return read_rows(connection, slug)

    monkeypatch.setattr(sqlite.SQLiteLedgerRepository, "get_document", counted_document)
    monkeypatch.setattr(sqlite, "read_rows", counted_rows)
    return loads


def test_a_single_resource_read_loads_no_whole_ledger(live, whole_loads):
    paths = seed()
    whole_loads.clear()
    for path in paths:
        status, reply = request(live, "GET", path)
        assert status == 200, (path, reply)
    assert whole_loads == []


def test_a_single_resource_read_answers_what_the_whole_ledger_read_answers(live):
    paths = seed()
    state = repository.get_document(SLUG)
    for path in paths:
        assert request(live, "GET", path) == (200, resources.read(state, path, {}))


@pytest.mark.parametrize("path", ["tasks/missing", "followups/missing/comments", "tasks/t1/answers"])
def test_a_single_resource_read_of_a_missing_item_answers_not_found(live, path):
    seed()
    assert request(live, "GET", path) == (404, {"error": {"code": "resource_missing", "message": "No such resource"}})
