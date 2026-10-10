import json
import sys
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.api import resources, routes
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.repository import hierarchy
from scripts.swarm_ledger.repository import sqlite as store

SLUG = "hierarchy-reads"
STATE = {
    "plans": [{"id": "a"}, {"id": "b"}],
    "phases": [
        {"id": "p1", "plan": "plans/a"},
        {"id": "p2", "plan": "plans/a", "done": True},
        {"id": "p3", "plan": "plans/b"},
        {"id": "p4"},
    ],
    "slices": [{"id": "s1", "phase": "phases/p1"}, {"id": "s2", "phase": "phases/p1"}],
    "tasks": [
        {"id": "t1", "phase": "p1", "slice": "slices/s1", "state": "done"},
        {"id": "t2", "phase": "p1", "slice": "slices/s2", "state": "claimed"},
        {"id": "t3", "phase": "p1", "state": "open"},
        {"id": "t4", "phase": "p1", "slice": "slices/s1", "state": "pr"},
        {"id": "t5", "phase": "p3", "state": "open"},
        {"id": "t6", "phase": "p4", "depends_on": ["t1"], "state": "open"},
        {"id": "t7", "state": "open", "out_of_scope": True},
        {"id": "t8", "phase": "p2", "depends_on": ["t6"], "state": "done"},
    ],
    "_meta": {"rev": 1},
}
PLAN_A = [
    ("plans/a", 0),
    ("phases/p1", 1),
    ("slices/s1", 2),
    ("tasks/t1", 3),
    ("tasks/t4", 3),
    ("slices/s2", 2),
    ("tasks/t2", 3),
    ("tasks/t3", 2),
    ("phases/p2", 1),
    ("tasks/t8", 2),
]


@pytest.fixture
def repo(tmp_path):
    found = store.SQLiteLedgerRepository(tmp_path / store.DATABASE)
    found.import_document(SLUG, STATE)
    return found


def shape(rows):
    return [(row["node"], row["depth"]) for row in rows]


def test_subtree_of_a_plan_lists_its_phases_slices_and_tasks_in_display_order(repo):
    rows = repo.nodes(SLUG, "subtree", "plans/a")
    assert shape(rows) == PLAN_A
    assert rows[1] == {"node": "phases/p1", "kind": "phase", "parent": "plans/a", "depth": 1}


def test_subtree_of_a_phase_starts_at_the_phase(repo):
    assert shape(repo.nodes(SLUG, "subtree", "phases/p1")) == [
        ("phases/p1", 0),
        ("slices/s1", 1),
        ("tasks/t1", 2),
        ("tasks/t4", 2),
        ("slices/s2", 1),
        ("tasks/t2", 2),
        ("tasks/t3", 1),
    ]


def test_subtree_of_a_slice_lists_its_tasks_in_order(repo):
    assert shape(repo.nodes(SLUG, "subtree", "slices/s1")) == [("slices/s1", 0), ("tasks/t1", 1), ("tasks/t4", 1)]


def test_subtree_of_a_lone_task_is_the_task(repo):
    assert repo.nodes(SLUG, "subtree", "tasks/t7") == [{"node": "tasks/t7", "kind": "task", "parent": None, "depth": 0}]


def test_subtree_of_the_ledger_lists_every_root_plans_first(repo):
    assert shape(repo.nodes(SLUG, "subtree")) == [
        *PLAN_A,
        ("plans/b", 0),
        ("phases/p3", 1),
        ("tasks/t5", 2),
        ("phases/p4", 0),
        ("tasks/t6", 1),
        ("tasks/t7", 0),
    ]


def test_children_put_slices_before_lone_tasks(repo):
    assert shape(repo.nodes(SLUG, "children", "phases/p1")) == [("slices/s1", 1), ("slices/s2", 1), ("tasks/t3", 1)]


def test_children_of_the_ledger_are_its_roots(repo):
    assert shape(repo.nodes(SLUG, "children")) == [("plans/a", 0), ("plans/b", 0), ("phases/p4", 0), ("tasks/t7", 0)]


def test_ancestors_run_from_the_root_down(repo):
    assert shape(repo.nodes(SLUG, "ancestors", "tasks/t1")) == [("plans/a", 3), ("phases/p1", 2), ("slices/s1", 1)]
    assert repo.nodes(SLUG, "ancestors", "plans/a") == []


def test_dependents_follow_the_requires_links_downstream(repo):
    assert shape(repo.nodes(SLUG, "dependents", "tasks/t1")) == [("tasks/t6", 1), ("tasks/t8", 2)]
    assert repo.nodes(SLUG, "dependents", "tasks/t8") == []


@pytest.mark.parametrize("read", sorted(hierarchy.READS))
def test_an_unknown_node_is_refused(repo, read):
    with pytest.raises(KeyError) as caught:
        repo.nodes(SLUG, read, "tasks/nope")
    assert caught.value.args == ("tasks/nope",)


def test_cycles_in_parents_and_dependencies_end(tmp_path):
    loop = store.SQLiteLedgerRepository(tmp_path / store.DATABASE)
    loop.import_document(
        SLUG,
        {
            "tasks": [
                {"id": "t1", "slice": "tasks/t1", "depends_on": ["t2"]},
                {"id": "t2", "depends_on": ["t1"]},
            ],
            "_meta": {"rev": 1},
        },
    )
    assert shape(loop.nodes(SLUG, "subtree")) == [("tasks/t2", 0)]
    assert len(loop.nodes(SLUG, "subtree", "tasks/t1")) <= 4
    assert len(loop.nodes(SLUG, "ancestors", "tasks/t1")) <= 3
    assert shape(loop.nodes(SLUG, "dependents", "tasks/t1")) == [("tasks/t2", 1)]


def server(repo):
    return SimpleNamespace(repository=repo)


def test_the_hierarchy_resource_reads_the_ledger_tree_with_states(repo):
    reply = routes.ledger_read(server(repo), SLUG, "hierarchy", {})
    assert [(row["node"], row["state"]) for row in reply["data"][:3]] == [
        ("plans/a", "open"),
        ("phases/p1", "building"),
        ("slices/s1", "open"),
    ]
    states = {row["node"]: row["state"] for row in reply["data"]}
    assert (states["phases/p2"], states["tasks/t2"], states["tasks/t8"]) == ("done", "claimed", "done")
    assert reply["revision"]


def test_the_hierarchy_resource_names_phase_lifecycles_and_skips_items_without_an_id(tmp_path):
    found = store.SQLiteLedgerRepository(tmp_path / store.DATABASE)
    found.import_document(
        SLUG,
        {
            "phases": [{"id": "p1", "planning": "auto"}, {"id": "p2", "depends_on": ["p1"]}, {"title": "no id"}],
            "slices": [{"id": "s1", "phase": "phases/p1", "done": True}],
            "_meta": {"rev": 1},
        },
    )
    reply = resources.hierarchy_read(found, SLUG, "hierarchy")
    assert [(row["node"], row["state"]) for row in reply["data"]] == [
        ("phases/p1", "to_plan"),
        ("slices/s1", "done"),
        ("phases/p2", "waiting"),
    ]


def test_the_hierarchy_resource_runs_each_read_on_a_node(repo):
    reply = resources.hierarchy_read(repo, SLUG, "hierarchy/subtree/slices/s1")
    assert [row["node"] for row in reply["data"]] == ["slices/s1", "tasks/t1", "tasks/t4"]
    reply = resources.hierarchy_read(repo, SLUG, "hierarchy/dependents/tasks/t1")
    assert [row["node"] for row in reply["data"]] == ["tasks/t6", "tasks/t8"]
    reply = routes.ledger_read(server(repo), SLUG, "hierarchy/children/phases/p1", {})
    assert [row["node"] for row in reply["data"]] == ["slices/s1", "slices/s2", "tasks/t3"]


def test_the_hierarchy_revision_follows_the_rows(repo):
    whole = resources.hierarchy_read(repo, SLUG, "hierarchy")["revision"]
    assert whole == resources.hierarchy_read(repo, SLUG, "hierarchy")["revision"]
    assert whole != resources.hierarchy_read(repo, SLUG, "hierarchy/subtree/slices/s1")["revision"]


def test_a_node_whose_item_left_the_document_reads_as_open():
    class Drifted:
        def nodes(self, slug, read, node=None):
            return [{"node": "tasks/gone", "kind": "task", "parent": None, "depth": 0}]

        def read(self, slug, *keys):
            return {"tasks": []}

    assert [row["state"] for row in resources.hierarchy_read(Drifted(), SLUG, "hierarchy")["data"]] == ["open"]


@pytest.mark.parametrize(
    ("path", "message"),
    [("hierarchy/subtree/tasks/nope", "No such node"), ("hierarchy/sideways/tasks/t1", "No such hierarchy read")],
)
def test_the_hierarchy_resource_answers_missing_for_an_unknown_read_or_node(repo, path, message):
    with pytest.raises(APIError) as caught:
        routes.ledger_read(server(repo), SLUG, path, {})
    assert (caught.value.status, caught.value.code, str(caught.value)) == (404, "resource_missing", message)


def test_a_hierarchy_read_adopts_only_its_own_legacy_ledger(repo, tmp_path):
    document = json.dumps(repo.get_document(SLUG))
    (tmp_path / "other-ledger.json").write_text(document)
    (tmp_path / "third-ledger.json").write_text(document)
    assert shape(repo.nodes("other-ledger", "children")) == [
        ("plans/a", 0),
        ("plans/b", 0),
        ("phases/p4", 0),
        ("tasks/t7", 0),
    ]
    assert not (tmp_path / "other-ledger.json").exists() and (tmp_path / "third-ledger.json").exists()


def test_tree_prints_the_subtree_indented_with_states(repo, monkeypatch, capsys):
    read = []

    def resource(slug, path):
        read.append((slug, path))
        return routes.ledger_read(server(repo), slug, path, {})["data"]

    monkeypatch.setattr(ledger, "resource", resource)
    ledger.cmd_tree(SimpleNamespace(slug=SLUG, node="slices/s1"))
    assert read == [(SLUG, "hierarchy/subtree/slices/s1")]
    assert capsys.readouterr().out.splitlines() == ["slices/s1  open", "  tasks/t1  done", "  tasks/t4  pr"]


def test_tree_without_a_node_prints_the_whole_ledger(repo, monkeypatch, capsys):
    monkeypatch.setattr(ledger, "resource", lambda slug, path: routes.ledger_read(server(repo), slug, path, {})["data"])
    ledger.cmd_tree(SimpleNamespace(slug=SLUG, node=None))
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "plans/a  open" and lines[-1] == "tasks/t7  out_of_scope" and len(lines) == 16


def test_tree_reads_without_a_member_name(repo, monkeypatch, capsys):
    monkeypatch.setattr(ledger, "resource", lambda slug, path: routes.ledger_read(server(repo), slug, path, {})["data"])
    monkeypatch.setattr(sys, "argv", ["ledger", "--slug", SLUG, "tree", "slices/s2"])
    ledger.main()
    assert capsys.readouterr().out.splitlines() == ["slices/s2  open", "  tasks/t2  claimed"]


def test_tree_takes_an_optional_node_and_documents_its_forms(capsys):
    parser = ledger.build_parser()
    assert parser.parse_args(["--slug", SLUG, "tree"]).node is None
    for argv in (["--help"], ["tree", "--help"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
    out = " ".join(capsys.readouterr().out.split())
    assert "tree print a plan, phase, slice or task and everything under it, with states" in out
    assert "node plans/<id>, phases/<id>, slices/<id> or tasks/<id>; default the whole ledger" in out
