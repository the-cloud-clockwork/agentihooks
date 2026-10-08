import pytest

from scripts.swarm_ledger import ledger, ledger_groups, ledger_tasks, new_ledger
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger.api import schemas

SLUG = "taskgroup-2026-01-01"
MASTER = "master@abcdef-0001"
ENGINEER = "engineer@abcdef-0002"


def task(task_id, **fields):
    return {
        "id": task_id,
        "lane": "eng",
        "state": "open",
        "claimed_by": "",
        "kind": "code",
        "difficulty": "S",
        **fields,
    }


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_add", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_update", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_group", ledger_groups)
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html_path, _ = core.paths(SLUG)
    html_path.write_text(new_ledger.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    ops = [
        {"op": "task_add", "id": f"seed-{n}", "by": "swarm", "task": f"t{n}", "title": f"task {n}", "lane": "eng"}
        for n in range(1, 8)
    ]
    ops.append({"op": "task_add", "id": "seed-ci", "by": "swarm", "task": "c1", "title": "ci", "lane": "ci"})
    core.sync(SLUG, ops=ops)
    for n in range(1, 8):
        size(f"t{n}", "S")
    size("c1", "S")


def size(task_id, difficulty):
    op = {
        "op": "task_update",
        "id": f"size-{task_id}-{difficulty}",
        "by": MASTER,
        "item": f"tasks/{task_id}",
        "fields": {"difficulty": difficulty},
    }
    core.sync(SLUG, ops=[op])


def group(lead, members, by="swarm", n=1):
    op = {"op": "task_group", "id": f"group-{n}", "by": by, "item": f"tasks/{lead}", "members": members}
    core.check_op(op)
    return core.sync(SLUG, ops=[op])


def rows(state):
    return {t["id"]: t for t in state["tasks"]}


def test_the_lead_lists_its_members_and_each_member_points_at_the_lead():
    state, rejected = group("t1", ["t2", "t3"])
    found = rows(state)
    assert rejected == []
    assert found["t1"]["group_members"] == ["t2", "t3"]
    assert (found["t2"]["merged_into"], found["t3"]["merged_into"]) == ("t1", "t1")
    assert "merged_into" not in found["t1"] and "group_members" not in found["t4"]
    assert state["_meta"]["events"][-1]["kind"] == "grouped"


def test_a_group_holds_at_most_five_tasks():
    with pytest.raises(ValueError) as refused:
        core.check_op(
            {
                "op": "task_group",
                "id": "g",
                "by": "swarm",
                "item": "tasks/t1",
                "members": ["t2", "t3", "t4", "t5", "t6"],
            }
        )
    assert str(refused.value) == "a group holds at most 5 tasks"
    state, rejected = group("t1", ["t2", "t3", "t4", "t5"])
    assert rejected == [] and len(rows(state)["t1"]["group_members"]) == 4


def test_the_combined_size_never_passes_m():
    size("t2", "M")
    state, rejected = group("t1", ["t2"])
    assert rejected == ["group-1"] and "group_members" not in rows(state)["t1"]
    assert "together they pass M" in state["_meta"]["warnings"][-1]


def test_an_l_task_never_joins_a_group():
    size("t2", "L")
    state, rejected = group("t1", ["t2"])
    assert rejected == ["group-1"]


@pytest.mark.parametrize(
    "tasks, reason",
    [
        ([task("a"), task("b", lane="ci")], "same lane, profile and kind"),
        ([task("a"), task("b", profile="frontend")], "same lane, profile and kind"),
        ([task("a"), task("b", state="claimed")], "open and unclaimed"),
        ([task("a"), task("b", claimed_by=ENGINEER)], "open and unclaimed"),
        ([task("a"), task("b", merged_into="z")], "already grouped"),
        ([task("a", group_members=["z"]), task("b")], "already grouped"),
        ([task("a", kind="ops"), task("b", kind="ops")], "code or ci"),
        ([task("a"), task("b", difficulty=None)], "needs a difficulty"),
        ([task("a"), task("b", depends_on=["a"])], "depends on"),
        ([task("a", depends_on=["x"]), task("b")], "depends on"),
        ([task("a"), task("b"), task("c"), task("d"), task("e"), task("f")], "at most 5 tasks"),
        ([task("a"), task("b", difficulty="M")], "together they pass M"),
        ([task("a", difficulty="L")], "together they pass M"),
    ],
)
def test_the_refusal_names_why_tasks_cannot_group(tasks, reason):
    known = {t["id"]: t for t in tasks} | {"x": task("x", depends_on=["b"])}
    assert reason in ledger_groups.refusal(tasks, known)


def test_a_task_waiting_on_an_open_task_outside_the_group_is_refused():
    tasks = [task("a"), task("b", depends_on=["y"])]
    known = {t["id"]: t for t in tasks} | {"y": task("y")}
    assert ledger_groups.refusal(tasks, known) == "task b waits on task y"


def test_tasks_that_qualify_have_no_refusal():
    tasks = [task("a"), task("b"), task("c", difficulty="S", depends_on=["z"])]
    known = {t["id"]: t for t in tasks} | {"z": task("z", state="done")}
    assert ledger_groups.refusal(tasks, known) == ""
    assert ledger_groups.refusal([task("a", difficulty="M")], {}) == ""


def test_a_lane_agent_cannot_group_tasks():
    state, rejected = group("t1", ["t2"], by=ENGINEER)
    assert rejected == ["group-1"] and "cannot set a task group" in state["_meta"]["warnings"][-1]


def test_the_master_groups_tasks():
    state, rejected = group("t1", ["t2"], by=MASTER)
    assert rejected == [] and rows(state)["t2"]["merged_into"] == "t1"


def test_an_unknown_task_or_another_lane_is_refused():
    state, rejected = group("t1", ["t9"])
    assert rejected == ["group-1"]
    state, rejected = group("t1", ["c1"], n=2)
    assert rejected == ["group-2"] and "same lane" in state["_meta"]["warnings"][-1]


def test_a_grouped_task_cannot_join_a_second_group():
    group("t1", ["t2"])
    state, rejected = group("t3", ["t2"], n=2)
    assert rejected == ["group-2"] and rows(state)["t2"]["merged_into"] == "t1"


@pytest.mark.parametrize(
    "op",
    [
        {"op": "task_group", "id": "g", "by": "swarm", "item": "tasks/t1", "members": []},
        {"op": "task_group", "id": "g", "by": "swarm", "item": "tasks/t1", "members": ["t1"]},
        {"op": "task_group", "id": "g", "by": "swarm", "item": "tasks/t1", "members": ["t2", "t2"]},
        {"op": "task_group", "id": "g", "by": "swarm", "item": "tasks/t1", "members": "t2"},
        {"op": "task_group", "id": "g", "by": "swarm", "item": "phases/p1", "members": ["t2"]},
        {"op": "task_group", "id": "g", "by": "operator", "item": "tasks/t1", "members": ["t2"]},
        {"op": "task_group", "id": "g", "item": "tasks/t1", "members": ["t2"]},
        {"op": "task_group", "id": "g", "by": "swarm", "item": "tasks/t1", "members": ["t2"], "extra": 1},
    ],
)
def test_a_malformed_group_op_is_refused(op):
    with pytest.raises(ValueError):
        core.check_op(op)


def test_the_core_and_the_server_schema_know_the_group_op():
    assert core.EXTENSION_OPS["task_group"].OPS == ("task_group",)
    schema = schemas.operation_schema("task_group")
    op = {"op": "task_group", "id": "g", "by": "swarm", "item": "tasks/t1", "members": ["t2"]}
    assert schemas.mismatched_field(schema, op) is None
    assert schemas.mismatched_field(schema, {**op, "members": "t2"}) == "members"


def test_task_group_cli_sends_the_lead_and_its_members(monkeypatch):
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **f: sent.append((kind, f)))
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", MASTER, "task", "group", "t1", "t2", "t3"])
    ledger.cmd_task(args)
    assert sent == [("task_group", {"item": "tasks/t1", "members": ["t2", "t3"]})]
