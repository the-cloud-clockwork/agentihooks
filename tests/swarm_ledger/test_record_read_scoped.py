"""A ledger read of one record reads that record's rows alone, never the whole stored ledger."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger.api import resources, routes
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.api.services import Services
from scripts.swarm_ledger.repository import sqlite as store
from tests.swarm_ledger.api_fakes import as_services

SLUG = "record-read-scoped"
STATE = {
    "phases": [{"id": "p1", "title": "Phase one"}],
    "tasks": [
        {"id": "t1", "title": "One", "comments": [{"by": "a", "text": "hi"}]},
        {"id": "t2", "title": "Two"},
    ],
    "_meta": {"rev": 1},
}


@pytest.fixture
def repo(tmp_path: Path) -> store.SQLiteLedgerRepository:
    """A stored ledger holding two tasks and a phase."""
    found = store.SQLiteLedgerRepository(tmp_path / store.DATABASE)
    found.import_document(SLUG, STATE)
    return found


def whole_load_refused(*_: object) -> object:
    """Stand in for the whole ledger load a single record read must not reach."""
    raise AssertionError("a single record read loaded the whole ledger")


def server(repo: store.SQLiteLedgerRepository) -> Services:
    """The ledger API services over repo."""
    return as_services(SimpleNamespace(repository=repo))


@pytest.mark.feature("ledger-record-read-scoped")
def test_a_record_read_never_loads_the_whole_ledger(
    repo: store.SQLiteLedgerRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = resources.read(repo.current(SLUG).state, "tasks/t1")
    fresh = store.SQLiteLedgerRepository(repo.path)
    monkeypatch.setattr(store, "load", whole_load_refused)
    monkeypatch.setattr(fresh, "current", whole_load_refused)
    assert routes.ledger_read(server(fresh), SLUG, "tasks/t1", {}) == expected


@pytest.mark.feature("ledger-record-read-scoped")
def test_a_record_read_answers_as_the_whole_ledger_read(repo: store.SQLiteLedgerRepository) -> None:
    state = repo.current(SLUG).state
    for path in ("tasks/t1", "tasks/t2", "phases/p1"):
        assert routes.ledger_read(server(repo), SLUG, path, {}) == resources.read(state, path)


@pytest.mark.feature("ledger-record-read-scoped")
def test_a_missing_record_read_is_refused_as_missing(repo: store.SQLiteLedgerRepository) -> None:
    with pytest.raises(APIError) as refused:
        routes.ledger_read(server(repo), SLUG, "tasks/nope", {})
    assert refused.value.status == 404


@pytest.mark.feature("ledger-record-read-scoped")
def test_only_one_record_of_a_record_collection_takes_the_scoped_read() -> None:
    assert routes.record_path("tasks/t1")
    assert not routes.record_path("tasks")
    assert not routes.record_path("tasks/")
    assert not routes.record_path("tasks/t1/comments")
    assert not routes.record_path("members/a")
