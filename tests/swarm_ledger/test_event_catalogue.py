from scripts.events import catalogue
from scripts.events.envelope import Envelope, create
from scripts.swarm_ledger.repository import outbox, sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

CONTENT = {
    "title": "Store",
    "overview": "o",
    "sources": [],
    "phases": [{"title": "One", "description": "d"}],
    "tasks": [
        {"title": "first", "phase": "p1", "lane": "eng"},
        {"title": "second", "phase": "p1", "lane": "eng"},
    ],
}
ADD = {"op": "task_add", "id": "a1", "by": "swarm", "task": "t3", "title": "third", "lane": "eng", "phase": "p1"}
CLAIM = {"op": "task_update", "id": "u1", "by": "eng", "item": "tasks/t2", "fields": {"state": "claimed"}}
APPEND = {"op": "phase_append", "id": "a2", "by": "operator", "phases": [{"phase": "p2", "title": "Two"}]}
DONE = {"path": "phases/p1/done", "value": True, "base": False}


def written(repo):
    with repo.connect() as connection:
        values = connection.execute("SELECT value FROM outbox ORDER BY revision, subject").fetchall()
    return [Envelope.decode(value.encode()) for (value,) in values]


def event(kind, entity, data):
    return create(type=kind, swarm="demo", entity=entity, key="t1", version=1, author="ledger", data=data)


def test_every_row_the_outbox_writers_write_matches_the_catalogue_and_every_catalogued_type_is_written(tmp_path):
    repo = SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    repo.create("ledger", CONTENT)
    writes = [{"ops": [ADD]}, {"ops": [CLAIM]}, {"ops": [APPEND]}, {"changes": [DONE]}]
    assert [repo.apply_ops("ledger", **write)[1] for write in writes] == [[], [], [], []]
    source = repo.get_document("ledger")
    repo.import_document("ledger", {**source, "tasks": source["tasks"][:2]}, replace=True)
    with repo.transaction() as transaction:
        transaction.purge("ledger")
    events = written(repo)
    assert sorted((item.type, item.key) for item in events if item.key in ("t3", "p2")) == [
        ("phase.changed", "p2"),
        ("phase.removed", "p2"),
        ("task.changed", "t3"),
        ("task.removed", "t3"),
    ]
    assert [(item.type, catalogue.refusal(item)) for item in events if catalogue.refusal(item)] == []
    assert {item.type for item in events} == set(catalogue.LEDGER)


def test_the_catalogue_names_the_entities_the_outbox_writes_with_a_changed_and_removed_type_each():
    assert sorted(catalogue.LEDGER) == ["phase.changed", "phase.removed", "task.changed", "task.removed"]
    assert {kind.entity for kind in catalogue.LEDGER.values()} == set(outbox.ENTITIES.values())
    assert catalogue.LEDGER["task.changed"].fields == frozenset(
        ("id", "title", "description", "phase", "lane", "state", "claimed_by", "issue_url", "pr_url", "done")
    )
    assert catalogue.LEDGER["phase.changed"].fields == frozenset(("id", "title", "description", "done"))
    assert catalogue.LEDGER["task.removed"] == catalogue.EventType("task", frozenset())
    assert catalogue.LEDGER["phase.removed"] == catalogue.EventType("phase", frozenset())


def test_refusal_passes_an_event_carrying_every_catalogued_field_and_more():
    phase = {"id": "p1", "title": "One", "description": "", "done": False, "planning": "auto"}
    assert catalogue.refusal(event("phase.changed", "phase", phase)) is None
    assert catalogue.refusal(event("task.removed", "task", {})) is None


def test_refusal_names_an_unknown_type_a_wrong_entity_missing_fields_and_data_on_a_removal():
    assert catalogue.refusal(event("task.moved", "task", {})) == "unknown ledger event type 'task.moved'"
    assert catalogue.refusal(event("task.removed", "phase", {})) == "task.removed is a task event, not phase"
    assert catalogue.refusal(event("phase.changed", "phase", {"id": "p1", "title": "One"})) == (
        "phase.changed data lacks description done"
    )
    assert catalogue.refusal(event("phase.removed", "phase", {"id": "p1"})) == "phase.removed carries no data"
