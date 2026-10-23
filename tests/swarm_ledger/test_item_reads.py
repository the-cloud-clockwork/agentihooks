import json
import sys
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.api import routes
from scripts.swarm_ledger.repository import sqlite as store
from tests.swarm_ledger.api_fakes import as_services

SLUG = "item-reads"
STATE = {
    "phases": [{"id": "p1", "title": "Build"}],
    "followups": [{"id": "f1", "text": "Speed up the shard"}],
    "priorities": [{"id": "r1", "text": "Pick a hive"}],
    "tasks": [
        {"id": "t1", "title": "Hive placement", "lane": "eng", "state": "open"},
        {"id": "t2", "title": "Queue speed", "lane": "ci", "state": "claimed"},
        {"id": "t3", "title": "Hive drain", "lane": "eng", "state": "claimed"},
        {"id": "t4", "title": "Hive docs", "lane": "eng", "state": "done"},
        {"id": "t5", "title": "Hive chart", "lane": "eng", "state": "open", "out_of_scope": True},
    ],
    "_meta": {"rev": 1},
}


@pytest.fixture
def reads(tmp_path, monkeypatch):
    repo = store.SQLiteLedgerRepository(tmp_path / store.DATABASE)
    repo.import_document(SLUG, STATE)
    services = as_services(SimpleNamespace(repository=repo))
    seen = []

    def resource(slug, path, collection=False):
        seen.append((slug, path, collection))
        return routes.ledger_read(services, slug, path, {})["data"]

    def whole(*_args, **_kwargs):
        raise AssertionError("an item read never loads the whole ledger")

    monkeypatch.setattr(ledger, "resource", resource)
    monkeypatch.setattr(ledger, "call", whole)
    return seen


@pytest.mark.parametrize(
    ("item", "field", "expected"),
    [
        ("tasks/t2", "title", "Queue speed"),
        ("phases/p1", "title", "Build"),
        ("followups/f1", "text", "Speed up the shard"),
        ("priorities/r1", "text", "Pick a hive"),
    ],
)
def test_get_prints_one_item_read_through_the_ledger_api(reads, capsys, monkeypatch, item, field, expected):
    monkeypatch.setattr(sys, "argv", ["ledger", "--slug", SLUG, "get", item])
    ledger.main()
    assert json.loads(capsys.readouterr().out)[field] == expected
    assert reads == [(SLUG, item, False)]


@pytest.mark.parametrize(
    "item", ["chat/c1", "tasks/", "tasks/a/b", "tasks/t1?state=done", "tasks/a#b", "tasks/.", "tasks/.."]
)
def test_get_refuses_an_item_outside_tasks_phases_followups_and_priorities(item):
    with pytest.raises(SystemExit):
        ledger.build_parser().parse_args(["--slug", SLUG, "get", item])


def test_tasks_lists_open_tasks_one_line_each_from_the_tasks_collection(reads, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ledger", "--slug", SLUG, "tasks"])
    ledger.main()
    assert capsys.readouterr().out.splitlines() == [
        "t1  open  eng  Hive placement",
        "t2  claimed  ci  Queue speed",
        "t3  claimed  eng  Hive drain",
    ]
    assert reads == [(SLUG, "tasks", True)]


@pytest.mark.parametrize(
    ("flags", "ids"),
    [
        (["--state", "claimed"], ["t2", "t3"]),
        (["--lane", "eng"], ["t1", "t3"]),
        (["--text", "hive"], ["t1", "t3"]),
        (["--state", "claimed", "--lane", "eng", "--text", "drain"], ["t3"]),
    ],
)
def test_tasks_filters_open_tasks_by_state_lane_and_title_text(reads, capsys, monkeypatch, flags, ids):
    monkeypatch.setattr(sys, "argv", ["ledger", "--slug", SLUG, "tasks", *flags])
    ledger.main()
    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()] == ids
