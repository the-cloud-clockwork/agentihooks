import json
import sys
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger, ledger_tasks, plan_ranges


@pytest.fixture
def doc(monkeypatch):
    monkeypatch.setattr(plan_ranges, "anchors", lambda doc, phase: ["first"] if phase.get("id") == "p1" else [])
    return {"phases": [{"id": "p1"}, {"id": "p2"}], "tasks": [{"id": "t1", "title": "Held", "state": "open"}]}


def serve(monkeypatch, doc, reply=None):
    sent = []

    def call(slug, ops):
        assert slug == "s" and len(ops) == 1
        sent.extend(ops)
        if reply is not None:
            return reply
        ctx = SimpleNamespace(refused=[], record=lambda *a, **k: None)
        accepted = ledger_tasks.apply(doc, dict(ops[0]), ctx)
        ids = [ops[0]["id"]]
        return {
            "applied": ids if accepted else [],
            "rejected": [] if accepted else ids,
            "_meta": {"warnings": ctx.refused},
        }

    monkeypatch.setattr(ledger, "call", call)
    return sent


def add(*argv, name="master"):
    args = ledger.build_parser().parse_args(["--slug", "s", "--as", name, "task", "add", *argv])
    ledger.cmd_task(args)


def refusal(monkeypatch, doc, *argv, name="master"):
    sent = serve(monkeypatch, doc)
    with pytest.raises(SystemExit) as exit_:
        add(*argv, name=name)
    assert [(op["op"], op["task"], op["by"]) for op in sent] == [("task_add", argv[0], name)]
    return str(exit_.value)


def test_a_task_add_missing_its_slice_exits_nonzero_and_says_it_was_not_added(monkeypatch, doc, capsys):
    text = refusal(monkeypatch, doc, "t9", "Fix", "--phase", "p1")
    assert text.startswith("task t9 was not added: phase p1 has a plan with slice anchors")
    assert capsys.readouterr().out == ""
    assert [t["id"] for t in doc["tasks"]] == ["t1"]


@pytest.mark.parametrize(
    ("argv", "name", "reason"),
    [
        (["t1", "Again", "--phase", "p2"], "master", 'task t1 already exists as "Held" (open)'),
        (["t9", "Fix", "--phase", "p2"], "engineer@d44a5a-0516", "engineer@d44a5a-0516 works in the eng lane"),
        (["t9", "Fix", "--phase", "p2", "--rank", "1"], "master", "master cannot set a task rank"),
        (
            ["t9", "Fix", "--phase", "p1", "--follow-up", "--plan-slice", "first"],
            "master",
            "task t9 is a follow up and names no slice",
        ),
        (
            ["t9", "Fix", "--phase", "p2", "--depends-on", "wraith,t1,ghost"],
            "master",
            "task t9 depends on unknown tasks: ghost, wraith",
        ),
    ],
)
def test_every_refused_task_add_exits_nonzero_and_says_it_was_not_added(monkeypatch, doc, argv, name, reason):
    text = refusal(monkeypatch, doc, *argv, name=name)
    assert text.startswith(f"task {argv[0]} was not added: {reason}")
    assert [t["id"] for t in doc["tasks"]] == ["t1"]


@pytest.mark.parametrize("reply", [{"applied": [], "_meta": {"warnings": []}}, {"_meta": {}}])
def test_a_task_add_the_server_neither_applied_nor_rejected_exits_nonzero(monkeypatch, doc, reply, capsys):
    serve(monkeypatch, doc, reply)
    with pytest.raises(SystemExit) as exit_:
        add("t9", "Fix", "--phase", "p2")
    assert str(exit_.value) == "task t9 was not added: the ledger server did not apply it"
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("kind", [SystemExit, ledger.Missing])
def test_a_task_add_the_server_refused_over_http_says_it_was_not_added(monkeypatch, kind):
    sent = []

    def call(slug, ops):
        sent.extend(ops)
        raise kind("server refused: 400 schema_invalid")

    monkeypatch.setattr(ledger, "call", call)
    with pytest.raises(kind) as exit_:
        add("t9", "Fix", "--phase", "p2")
    assert str(exit_.value) == "task t9 was not added: server refused: 400 schema_invalid"
    assert [op["task"] for op in sent] == ["t9"]


def test_a_refused_task_add_leaves_out_the_unchecked_duplicate_notice(monkeypatch, doc):
    unchecked = f"{ledger.ledger_task_duplicates.UNCHECKED_PREFIX} for t9"
    serve(monkeypatch, doc, {"rejected": ["x"], "_meta": {"warnings": [unchecked, "no room"]}})
    with pytest.raises(SystemExit) as exit_:
        add("t9", "Fix", "--phase", "p2")
    assert str(exit_.value) == "task t9 was not added: no room"


def test_a_task_add_the_server_applied_prints_its_id_and_returns(monkeypatch, doc, capsys):
    sent = serve(monkeypatch, doc)
    add("t9", "Fix", "--phase", "p2")
    assert json.loads(capsys.readouterr().out) == {"task": "t9", "added": "Fix"}
    assert [op["op"] for op in sent] == ["task_add"]
    assert [t["id"] for t in doc["tasks"]] == ["t1", "t9"]


def test_other_ops_keep_their_refusal_text_and_need_no_applied_list(monkeypatch):
    args = SimpleNamespace(slug="s", name="master")
    sent = serve(monkeypatch, {}, {"_meta": {}})
    assert ledger.send(args, "comment", item="tasks/t1", text="Note") == {"_meta": {}}
    assert [(op["op"], op["item"], op["text"]) for op in sent] == [("comment", "tasks/t1", "Note")]
    unchecked = f"{ledger.ledger_task_duplicates.UNCHECKED_PREFIX} for t1"
    serve(monkeypatch, {}, {"rejected": ["x"], "_meta": {"warnings": [unchecked, "no such item"]}})
    with pytest.raises(SystemExit) as exit_:
        ledger.send(args, "comment", item="tasks/t1", text="Note")
    assert str(exit_.value) == f"{unchecked}; no such item"
    monkeypatch.setattr(ledger, "call", lambda slug, ops: sys.exit("server refused: 404 gone"))
    with pytest.raises(SystemExit) as exit_:
        ledger.send(args, "comment", item="tasks/t1", text="Note")
    assert str(exit_.value) == "server refused: 404 gone"


def test_a_task_add_keeps_a_numeric_exit_code(monkeypatch):
    monkeypatch.setattr(ledger, "call", lambda slug, ops: sys.exit(3))
    with pytest.raises(SystemExit) as exit_:
        add("t9", "Fix", "--phase", "p2")
    assert exit_.value.code == 3
