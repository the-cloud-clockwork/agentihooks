import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_splits, ledger_tasks, new_ledger
from scripts.swarm_ledger.api import schemas
from tests.swarm_ledger import legacy_page

pytestmark = pytest.mark.unit

SLUG = "tasksplit-2026-01-01"
MASTER = "master@abcdef-0001"
ENGINEER = "engineer@abcdef-0002"


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html_path, _ = legacy_page.paths(SLUG, core)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    ops = [
        {"op": "task_add", "id": "seed-1", "by": "swarm", "task": "t1", "title": "task one", "lane": "eng"},
        {"op": "task_add", "id": "seed-r", "by": "swarm", "task": "r1", "title": "research", "lane": "eng"},
    ]
    core.sync(SLUG, ops=ops)
    update("r1", {"kind": "research"})


def update(task_id, fields, by=MASTER):
    op = {
        "op": "task_update",
        "id": f"u-{task_id}-{len(fields)}",
        "by": by,
        "item": f"tasks/{task_id}",
        "fields": fields,
    }
    return core.sync(SLUG, ops=[op])


def split(task_id, halves, by=MASTER, n=1):
    op = {"op": "task_split", "id": f"split-{n}", "by": by, "item": f"tasks/{task_id}", "halves": halves}
    core.check_op(op)
    return core.sync(SLUG, ops=[op])


def row(state, task_id):
    return next(t for t in state["tasks"] if t["id"] == task_id)


def test_the_master_records_an_ordered_split_on_a_code_task():
    state, rejected = split("t1", ["12-14", "15-20"])
    assert rejected == []
    assert row(state, "t1")["split"] == ["12-14", "15-20"]
    event = state["_meta"]["events"][-1]
    assert (event["by"], event["kind"], event["target"], event["text"]) == (
        MASTER,
        "split",
        "tasks/t1",
        "12-14, 15-20",
    )
    assert state["_meta"]["stamps"]["tasks/t1/split"]["by"] == MASTER


def test_a_lane_agent_cannot_split_a_task():
    state, rejected = split("t1", ["1-2", "3-4"], by=ENGINEER)
    assert rejected == ["split-1"] and "split" not in row(state, "t1")
    assert f"{ENGINEER}: eng lane, no split" in state["_meta"]["warnings"][-1]


def test_only_a_code_or_ci_task_is_split():
    state, rejected = split("r1", ["1-2", "3-4"])
    assert rejected == ["split-1"] and "split" not in row(state, "r1")
    assert "tasks/r1: not a code or ci task" in state["_meta"]["warnings"][-1]


def test_a_split_with_bound_pull_requests_is_not_rewritten():
    split("t1", ["1-2", "3-4"])
    update("t1", {"split_pulls": ["https://github.com/o/r/pull/9"]}, by=ENGINEER)
    state, rejected = split("t1", ["1-3", "4-4"], n=2)
    assert rejected == ["split-2"] and row(state, "t1")["split"] == ["1-2", "3-4"]
    assert "tasks/t1: a half has a pull request" in state["_meta"]["warnings"][-1]


def test_a_task_with_a_recorded_pull_request_is_not_split():
    update("t1", {"pr_url": "https://github.com/o/r/pull/9"}, by=ENGINEER)
    state, rejected = split("t1", ["1-2", "3-4"])
    assert rejected == ["split-1"] and "split" not in row(state, "t1")
    assert "tasks/t1: the task has a pull request outside the split" in state["_meta"]["warnings"][-1]


def test_an_unknown_task_is_skipped():
    _, rejected = split("t9", ["1-2", "3-4"])
    assert rejected == ["split-1"]


@pytest.mark.parametrize(
    "halves, reason",
    [
        (["1-2"], "halves: 2 to 4"),
        (["1-2", "3-4", "5-6", "7-8", "9-9"], "halves: 2 to 4"),
        ("1-2", "halves: 2 to 4"),
        (["1-2", "x"], "halves: plan line ranges"),
        (["4-5", "1-2"], "halves: ordered, not overlapping"),
        (["1-3", "3-4"], "halves: ordered, not overlapping"),
        (["repair", "1-2"], "halves: plan line ranges"),
        (["repair", ""], "halves: plan line ranges"),
        (["repair", "repair"], "halves: distinct labels"),
    ],
)
def test_a_malformed_split_is_refused(halves, reason):
    with pytest.raises(ValueError) as refused:
        core.check_op({"op": "task_split", "id": "s", "by": MASTER, "item": "tasks/t1", "halves": halves})
    assert str(refused.value) == reason


@pytest.mark.parametrize(
    "op, reason",
    [
        ({"by": "operator"}, "task_split by: an agent name"),
        ({"by": "1 master"}, "task_split by: an agent name"),
        ({"item": "phases/p1"}, "task_split fields: id, by, item tasks/<id>, halves"),
        ({"extra": 1}, "task_split fields: id, by, item tasks/<id>, halves"),
    ],
)
def test_the_op_shape_is_checked(op, reason):
    base = {"op": "task_split", "id": "s", "by": MASTER, "item": "tasks/t1", "halves": ["1-2", "3-4"]}
    with pytest.raises(ValueError) as refused:
        ledger_splits.check({**base, **op})
    assert str(refused.value) == reason


def test_the_api_schema_takes_halves_as_a_list_of_strings():
    schema = schemas.operation_schema("task_split")
    assert schema["properties"]["halves"]["type"] == "array"
    assert schemas.mismatched_field(schema, {"op": "task_split", "id": "s", "halves": [1]}) == "halves"


def test_split_pulls_is_a_list_field_a_worker_updates():
    assert "split_pulls" in ledger_tasks.UPDATABLE and "split_pulls" in ledger_tasks.LIST_FIELDS
    state, rejected = update("t1", {"split_pulls": ["https://github.com/o/r/pull/9"]}, by=ENGINEER)
    assert rejected == [] and row(state, "t1")["split_pulls"] == ["https://github.com/o/r/pull/9"]


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com/o/r/pull/9,https://github.com/o/r/pull/10",
        '["https://github.com/o/r/pull/9", "https://github.com/o/r/pull/10"]',
        '  ["https://github.com/o/r/pull/9", "https://github.com/o/r/pull/10"]',
    ],
    ids=["comma", "json", "indented-json"],
)
def test_task_set_stores_split_pull_addresses_as_a_list(value, monkeypatch, capsys):
    from scripts.swarm_ledger import ledger

    def send(args, kind, **fields):
        assert args.slug == SLUG
        operation = ledger.op(kind, args, **fields)
        ledger_tasks.check(operation)
        state, rejected = core.sync(SLUG, ops=[operation])
        assert rejected == []
        assert row(state, "t1")["split_pulls"] == [
            "https://github.com/o/r/pull/9",
            "https://github.com/o/r/pull/10",
        ]

    monkeypatch.setattr(ledger, "send", send)
    args = ledger.build_parser().parse_args(
        ["--slug", SLUG, "--as", ENGINEER, "task", "set", "t1", f"split_pulls={value}"]
    )
    ledger.cmd_task(args)
    assert row(core.sync(SLUG)[0], "t1")["split_pulls"] == [
        "https://github.com/o/r/pull/9",
        "https://github.com/o/r/pull/10",
    ]
    assert (
        '"split_pulls": ["https://github.com/o/r/pull/9", "https://github.com/o/r/pull/10"]' in capsys.readouterr().out
    )


def test_task_set_preserves_bracketed_territory_as_a_comma_list(monkeypatch):
    from scripts.swarm_ledger import ledger

    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **fields: sent.append(fields))
    args = ledger.build_parser().parse_args(["task", "set", "t1", "territory=[local],remote"])
    ledger.cmd_task(args)
    assert sent == [{"item": "tasks/t1", "fields": {"territory": ["[local]", "remote"]}}]


def test_task_split_cli_sends_the_ordered_halves(monkeypatch, capsys):
    from scripts.swarm_ledger import ledger

    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **f: sent.append((args, kind, f)))
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", MASTER, "task", "split", "t1", "1-2", "3-4"])
    ledger.cmd_task(args)
    assert sent == [(args, "task_split", {"item": "tasks/t1", "halves": ["1-2", "3-4"]})]
    assert capsys.readouterr().out == '{"task": "t1", "split": ["1-2", "3-4"]}\n'


@pytest.mark.parametrize(
    "halves, reason",
    [
        (["12-14", "15-20"], ""),
        (["12-12", "13-19", "20-20"], ""),
        (["12-14", "16-20"], "halves: cover plan lines 12-20"),
        (["10-14", "15-20"], "halves: cover plan lines 12-20"),
        (["12-14", "15-19"], "halves: cover plan lines 12-20"),
        (["12-14", "15-22"], "halves: cover plan lines 12-20"),
    ],
)
def test_a_split_covers_the_task_plan_lines_exactly(halves, reason):
    assert ledger_splits.refusal({"kind": "code", "plan_lines": "12-20"}, halves) == reason


def test_a_task_without_plan_lines_takes_any_ordered_split():
    assert ledger_splits.refusal({"kind": "code"}, ["1-2", "9-9"]) == ""


def test_a_split_takes_up_to_four_halves():
    op = {"op": "task_split", "id": "s", "by": MASTER, "item": "tasks/t1", "halves": ["1-2", "3-4", "5-6", "7-8"]}
    assert ledger_splits.check(op) is None


def test_the_ledger_refuses_a_split_that_misses_plan_lines(monkeypatch):
    real = ledger_splits.refusal
    monkeypatch.setattr(ledger_splits, "refusal", lambda task, halves: real({**task, "plan_lines": "1-4"}, halves))
    state, rejected = split("t1", ["1-2", "4-4"])
    assert rejected == ["split-1"] and "split" not in row(state, "t1")
    assert "tasks/t1: halves: cover plan lines 1-4" in state["_meta"]["warnings"][-1]
    state, rejected = split("t1", ["1-2", "3-4"], n=2)
    assert rejected == [] and row(state, "t1")["split"] == ["1-2", "3-4"]


def test_a_task_without_a_plan_records_labelled_halves_in_order():
    state, rejected = split("t1", ["compatibility", "repair"])
    assert rejected == [] and row(state, "t1")["split"] == ["compatibility", "repair"]
    pulls = ["https://github.com/o/r/pull/9", "https://github.com/o/r/pull/10"]
    state, rejected = update("t1", {"split_pulls": pulls}, by=ENGINEER)
    assert rejected == [] and row(state, "t1")["split_pulls"] == pulls


@pytest.mark.parametrize("plan", [{"plan_lines": "12-20"}, {"plan_slice": "SV2-01"}])
def test_a_planned_task_still_needs_plan_line_ranges(plan):
    assert ledger_splits.refusal({"kind": "code", **plan}, ["compatibility", "repair"]) == "halves: plan line ranges"
    assert ledger_splits.refusal({"kind": "code"}, ["compatibility", "repair"]) == ""
