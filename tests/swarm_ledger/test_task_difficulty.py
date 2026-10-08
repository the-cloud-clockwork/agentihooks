import json
import subprocess

import pytest

from scripts.swarm_ledger import ledger, ledger_tasks, new_ledger
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger.api import schemas
from tests.swarm_ledger import legacy_page

SLUG = "taskdifficulty-2026-01-01"
MASTER = "master@abcdef-0001"
ENGINEER = "engineer@abcdef-0002"


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_add", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_update", ledger_tasks)
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html_path, _ = core.paths(SLUG)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    core.sync(SLUG, ops=[{"op": "task_add", "id": "seed", "by": "swarm", "task": "t1", "title": "a", "lane": "eng"}])


def sized(state, task_id="t1"):
    task = next(t for t in state["tasks"] if t["id"] == task_id)
    return task.get("difficulty"), task.get("difficulty_source"), task.get("difficulty_confidence")


def update(by, fields, n=1):
    op = {"op": "task_update", "id": f"size-{n}", "by": by, "item": "tasks/t1", "fields": fields}
    core.check_op(op)
    return core.sync(SLUG, ops=[op])


def test_an_existing_task_has_no_difficulty():
    state, _ = core.sync(SLUG)
    assert sized(state) == (None, None, None)


def test_task_add_stores_the_difficulty_as_an_operator_choice():
    op = {"op": "task_add", "id": "add-2", "by": MASTER, "task": "t2", "title": "b", "lane": "eng", "difficulty": "S"}
    core.check_op(op)
    state, rejected = core.sync(SLUG, ops=[op])
    assert (rejected, sized(state, "t2")) == ([], ("S", "operator", 1.0))


def test_task_add_keeps_a_given_source_and_confidence():
    op = {
        "op": "task_add",
        "id": "add-3",
        "by": "swarm",
        "task": "t3",
        "title": "c",
        "lane": "eng",
        "difficulty": "L",
        "difficulty_source": "rule",
        "difficulty_confidence": 0.75,
    }
    core.check_op(op)
    state, rejected = core.sync(SLUG, ops=[op])
    assert (rejected, sized(state, "t3")) == ([], ("L", "rule", 0.75))


def test_the_master_override_records_source_operator():
    state, rejected = update(
        "swarm", {"difficulty": "M", "difficulty_source": "classifier", "difficulty_confidence": 0.8}
    )
    assert (rejected, sized(state)) == ([], ("M", "classifier", 0.8))
    state, rejected = update(MASTER, {"difficulty": "L"}, 2)
    assert (rejected, sized(state)) == ([], ("L", "operator", 1.0))


def test_an_engineer_cannot_size_a_task():
    state, rejected = update(ENGINEER, {"difficulty": "S"})
    assert rejected == ["size-1"] and sized(state) == (None, None, None)
    assert state["_meta"]["warnings"][0] == (
        f"{ENGINEER} works in the eng lane and cannot set a task difficulty: {ledger_tasks.PROPOSE}"
    )


@pytest.mark.parametrize("bad", ["XL", "", "s", "medium", 1, None])
def test_task_set_refuses_any_other_difficulty(bad):
    with pytest.raises(ValueError, match="difficulty must be one of"):
        core.check_op({"op": "task_update", "id": "x", "by": MASTER, "item": "tasks/t1", "fields": {"difficulty": bad}})
    with pytest.raises(ValueError, match="difficulty must be one of"):
        core.check_op(
            {"op": "task_add", "id": "y", "by": MASTER, "task": "t9", "title": "z", "lane": "eng", "difficulty": bad}
        )


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        (
            {"difficulty": "S", "difficulty_source": "guess"},
            "difficulty_source must be one of ('operator', 'rule', 'classifier', 'default')",
        ),
        ({"difficulty": "S", "difficulty_confidence": 1.5}, "difficulty_confidence must be a number from 0 to 1"),
        ({"difficulty": "S", "difficulty_confidence": -0.1}, "difficulty_confidence must be a number from 0 to 1"),
        ({"difficulty": "S", "difficulty_confidence": True}, "difficulty_confidence must be a number from 0 to 1"),
        ({"difficulty": "S", "difficulty_confidence": "0.5"}, "difficulty_confidence must be a number from 0 to 1"),
        ({"difficulty_source": "rule"}, "difficulty_source and difficulty_confidence come with a difficulty"),
        ({"difficulty_confidence": 0.5}, "difficulty_source and difficulty_confidence come with a difficulty"),
    ],
)
def test_source_and_confidence_are_checked(fields, message):
    with pytest.raises(ValueError) as refused:
        core.check_op({"op": "task_update", "id": "x", "by": MASTER, "item": "tasks/t1", "fields": fields})
    assert str(refused.value) == message


def test_a_seeded_task_with_a_bad_difficulty_is_refused():
    with pytest.raises(ValueError) as refused:
        ledger_tasks.check_task({"id": "t1", "difficulty": "XL"})
    assert str(refused.value) == "difficulty must be one of ('S', 'M', 'L')"


def test_task_add_help_names_the_difficulty(capsys):
    with pytest.raises(SystemExit):
        ledger.build_parser().parse_args(["task", "--help"])
    assert "--difficulty {S,M,L} task size: S, M or L" in " ".join(capsys.readouterr().out.split())


def test_the_server_schema_accepts_a_task_add_difficulty():
    schema = schemas.operation_schema("task_add")
    op = {"op": "task_add", "id": "a", "difficulty": "M", "difficulty_source": "default", "difficulty_confidence": 0.5}
    assert schemas.mismatched_field(schema, op) is None
    for bad in ("high", 1.5, -0.1):
        assert schemas.mismatched_field(schema, {**op, "difficulty_confidence": bad}) == "difficulty_confidence"
    for edge in (0, 1):
        assert schemas.mismatched_field(schema, {**op, "difficulty_confidence": edge}) is None


@pytest.mark.parametrize("edge", [0, 1])
def test_a_confidence_at_either_bound_is_kept(edge):
    state, rejected = update(
        "swarm", {"difficulty": "M", "difficulty_source": "classifier", "difficulty_confidence": edge}
    )
    assert (rejected, sized(state)) == ([], ("M", "classifier", edge))


def test_task_cli_sends_the_difficulty(monkeypatch):
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **f: sent.append((kind, f)))
    for argv in (
        ["task", "add", "t3", "b", "--difficulty", "L"],
        ["task", "add", "t4", "c"],
        ["task", "set", "t4", "difficulty=S"],
        ["task", "set", "t4", "difficulty=M", "difficulty_source=classifier", "difficulty_confidence=0.7"],
    ):
        ledger.cmd_task(ledger.build_parser().parse_args(["--slug", SLUG, "--as", MASTER, *argv]))
    assert [f.get("difficulty") for _, f in sent[:2]] == ["L", None]
    assert sent[2][1]["fields"] == {"difficulty": "S"}
    assert sent[3][1]["fields"] == {"difficulty": "M", "difficulty_source": "classifier", "difficulty_confidence": 0.7}


def test_task_cli_refuses_an_unknown_difficulty_flag():
    with pytest.raises(SystemExit):
        ledger.build_parser().parse_args(
            ["--slug", SLUG, "--as", MASTER, "task", "add", "t3", "b", "--difficulty", "XL"]
        )


def test_task_set_cli_refuses_a_confidence_that_is_not_a_number(monkeypatch):
    monkeypatch.setattr(ledger, "send", lambda *a, **k: None)
    args = ledger.build_parser().parse_args(
        ["--slug", SLUG, "--as", MASTER, "task", "set", "t1", "difficulty=S", "difficulty_confidence=high"]
    )
    with pytest.raises(SystemExit) as refused:
        ledger.cmd_task(args)
    assert refused.value.code == "task set takes difficulty_confidence as a number from 0 to 1"


def label(task):
    from tests.swarm_ledger.test_task_proof_page import function_source

    script = (
        "const h = (tag, attrs) => ({ tag, ...attrs });"
        + function_source("difficultyLabel")
        + f"process.stdout.write(JSON.stringify(difficultyLabel({json.dumps(task)})));"
    )
    return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)


@pytest.mark.parametrize(
    ("task", "hover"),
    [
        (
            {"difficulty": "L", "difficulty_source": "operator", "difficulty_confidence": 1},
            "set by operator, confidence 100%",
        ),
        (
            {"difficulty": "S", "difficulty_source": "classifier", "difficulty_confidence": 0.62},
            "set by classifier, confidence 62%",
        ),
        ({"difficulty": "M"}, "set by operator"),
    ],
)
def test_the_difficulty_label_names_its_source_and_confidence(task, hover):
    assert label(task) == {
        "tag": "span",
        "class": f"difficulty difficulty-{task['difficulty']}",
        "text": f"size {task['difficulty']}",
        "title": hover,
    }


def test_a_task_row_shows_its_difficulty():
    from tests.swarm_ledger.test_task_proof_page import PLAIN, nodes, render

    (shown,) = nodes(render({**PLAIN, "difficulty": "L"})["tree"], "difficulty difficulty-L")
    assert shown[2] == "size L"


def test_a_task_row_without_a_difficulty_shows_none():
    from tests.swarm_ledger.test_task_proof_page import PLAIN, render

    assert label(PLAIN) is None
    assert "difficulty" not in str(render(PLAIN)["tree"])
