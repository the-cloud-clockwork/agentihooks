import pytest

from scripts.swarm_ledger import ledger, ledger_groups, ledger_tasks, new_ledger
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger.api import schemas
from scripts.swarm_ledger.repository import repository
from tests.swarm_ledger import legacy_page

SLUG = "taskgroup-2026-01-01"
MASTER = "master@abcdef-0001"
ENGINEER = "engineer@abcdef-0002"
PLANNER = "planner@abcdef-0003"
DISPATCHER = "dispatcher@abcdef-0005"


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
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
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
    event = state["_meta"]["events"][-1]
    assert (event["by"], event["kind"], event["target"], event["text"]) == ("swarm", "grouped", "tasks/t1", "t2, t3")
    stamps = state["_meta"]["stamps"]
    assert [
        stamps[path]["by"] for path in ("tasks/t1/group_members", "tasks/t2/merged_into", "tasks/t3/merged_into")
    ] == ["swarm"] * 3


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
        ([task("a"), task("b", lane="ci")], "grouped tasks share the same lane, profile and kind"),
        ([task("a"), task("b", profile="frontend")], "grouped tasks share the same lane, profile and kind"),
        ([task("a"), task("b", state="claimed")], "task b is not open and unclaimed"),
        ([task("a"), task("b", claimed_by=ENGINEER)], "task b is not open and unclaimed"),
        ([task("a"), task("b", merged_into="z")], "task b is already grouped"),
        ([task("a", group_members=["z"]), task("b")], "task a is already grouped"),
        ([task("a", kind="ops"), task("b", kind="ops")], "task a is not a code or ci task"),
        ([task("a"), task("b", difficulty=None)], "task b needs a difficulty first"),
        ([task("a"), task("b", depends_on=["a"])], "task b depends on task a"),
        ([task("a", depends_on=["x"]), task("b")], "task a depends on task b"),
        ([task("a"), task("b", depends_on=["x", "y"])], "task b depends on task a"),
        ([task("a"), task("b", depends_on=["missing"])], "task b waits on task missing"),
        ([task("a"), task("b"), task("c"), task("d"), task("e"), task("f")], "a group holds at most 5 tasks"),
        ([task("a"), task("b", difficulty="M")], "together they pass M"),
        ([task("a", difficulty="L")], "together they pass M"),
    ],
)
def test_the_refusal_names_why_tasks_cannot_group(tasks, reason):
    chain = {
        "x": task("x", depends_on=["b"] if tasks[0].get("depends_on") else ["a"]),
        "y": task("y", depends_on=["z"]),
    }
    known = {t["id"]: t for t in tasks} | chain | {"z": task("z", state="done")}
    assert ledger_groups.refusal(tasks, known) == reason


def test_a_task_waiting_on_an_open_task_outside_the_group_is_refused():
    tasks = [task("a"), task("b", depends_on=["y"])]
    known = {t["id"]: t for t in tasks} | {"y": task("y")}
    assert ledger_groups.refusal(tasks, known) == "task b waits on task y"


def test_tasks_that_qualify_have_no_refusal():
    tasks = [task("a"), task("b"), task("c", difficulty="S", depends_on=["z"])]
    known = {t["id"]: t for t in tasks} | {"z": task("z", state="done")}
    assert ledger_groups.refusal(tasks, known) == ""
    assert ledger_groups.refusal([task("a", difficulty="M")], {}) == ""


@pytest.mark.parametrize(("by", "lane"), [(ENGINEER, "eng"), (PLANNER, "plan")])
def test_a_lane_agent_cannot_group_tasks(by, lane):
    state, rejected = group("t1", ["t2"], by=by)
    assert rejected == ["group-1"] and "merged_into" not in rows(state)["t2"]
    assert state["_meta"]["warnings"][-1] == f"{by} works in the {lane} lane and cannot set a task group"


def test_a_member_that_depends_on_the_lead_is_refused_by_the_ledger():
    op = {"op": "task_add", "id": "seed-8", "by": "swarm", "task": "t8", "title": "task 8", "lane": "eng"}
    core.sync(SLUG, ops=[{**op, "depends_on": ["t1"]}])
    size("t8", "S")
    state, rejected = group("t1", ["t8"])
    assert rejected == ["group-1"]
    assert state["_meta"]["warnings"][-1].endswith("tasks/t1 cannot lead this group: task t8 depends on task t1")


@pytest.mark.parametrize("by", [MASTER, DISPATCHER, "dispatcher"])
def test_the_master_and_the_dispatcher_group_tasks(by):
    state, rejected = group("t1", ["t2"], by=by)
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


BY = "task_group needs `by`, an agent name other than operator"
SHAPE = "task_group takes only an id, by, an item tasks/<lead id> and members"
MEMBERS = "members must list distinct task ids other than the lead"


@pytest.mark.parametrize(
    "op, message",
    [
        ({"op": "task_group", "id": "g", "by": "swarm", "item": "tasks/t1", "members": []}, MEMBERS),
        ({"op": "task_group", "id": "g", "by": "swarm", "item": "tasks/t1", "members": ["t1"]}, MEMBERS),
        ({"op": "task_group", "id": "g", "by": "swarm", "item": "tasks/t1", "members": ["t2", "t2"]}, MEMBERS),
        ({"op": "task_group", "id": "g", "by": "swarm", "item": "tasks/t1", "members": "t2"}, MEMBERS),
        ({"op": "task_group", "id": "g", "by": "swarm", "item": "tasks/t1", "members": ["-t2"]}, MEMBERS),
        ({"op": "task_group", "id": "g", "by": "swarm", "item": "phases/p1", "members": ["t2"]}, SHAPE),
        ({"op": "task_group", "id": "g", "by": "operator", "item": "tasks/t1", "members": ["t2"]}, BY),
        ({"op": "task_group", "id": "g", "item": "tasks/t1", "members": ["t2"]}, BY),
        ({"op": "task_group", "id": "g", "by": "swarm", "item": "tasks/t1", "members": ["t2"], "extra": 1}, SHAPE),
    ],
)
def test_a_malformed_group_op_is_refused(op, message):
    with pytest.raises(ValueError) as refused:
        ledger_groups.check(op)
    assert str(refused.value) == message


def test_the_core_and_the_server_schema_know_the_group_op():
    assert core.EXTENSION_OPS["task_group"] is ledger_groups
    schema = schemas.operation_schema("task_group")
    op = {"op": "task_group", "id": "g", "by": "swarm", "item": "tasks/t1", "members": ["t2"]}
    assert schemas.mismatched_field(schema, op) is None
    assert schemas.mismatched_field(schema, {**op, "members": "t2"}) == "members"


def test_task_group_cli_sends_the_lead_and_its_members(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **f: sent.append((args, kind, f)))
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", MASTER, "task", "group", "t1", "t2", "t3"])
    ledger.cmd_task(args)
    assert sent == [(args, "task_group", {"item": "tasks/t1", "members": ["t2", "t3"]})]
    assert capsys.readouterr().out == '{"task": "t1", "group_members": ["t2", "t3"]}\n'


def test_the_task_command_takes_only_add_set_or_group():
    with pytest.raises(SystemExit):
        ledger.build_parser().parse_args(["--slug", SLUG, "--as", MASTER, "task", "merge", "t1", "t2"])


def ungroup(lead, by="swarm", n=1):
    op = {"op": "task_ungroup", "id": f"ungroup-{n}", "by": by, "item": f"tasks/{lead}"}
    core.check_op(op)
    return core.sync(SLUG, ops=[op])


@pytest.fixture
def with_ungroup(monkeypatch):
    monkeypatch.setitem(core.EXTENSION_OPS, "task_ungroup", ledger_groups)


@pytest.mark.parametrize("by", [MASTER, DISPATCHER])
def test_ungroup_clears_the_lead_and_every_member_pointing_at_it(with_ungroup, by):
    group("t1", ["t2", "t3"])
    state, rejected = ungroup("t1", by=by)
    found = rows(state)
    assert rejected == []
    assert "group_members" not in found["t1"]
    assert "merged_into" not in found["t2"] and "merged_into" not in found["t3"]
    event = state["_meta"]["events"][-1]
    assert (event["by"], event["kind"], event["target"], event["text"]) == (by, "ungrouped", "tasks/t1", "t2, t3")
    stamps = state["_meta"]["stamps"]
    assert [stamps[f"tasks/{t}"]["by"] for t in ("t1/group_members", "t2/merged_into", "t3/merged_into")] == [
        MASTER
    ] * 3


def test_ungroup_skips_a_member_missing_from_the_ledger(with_ungroup):
    group("t1", ["t2"])
    doc = repository.bound(core).export_document(SLUG)
    next(t for t in doc["tasks"] if t["id"] == "t1")["group_members"].append("t9")
    repository.bound(core).import_document(SLUG, doc, replace=True)
    state, rejected = ungroup("t1")
    assert rejected == [] and "merged_into" not in rows(state)["t2"]
    assert state["_meta"]["events"][-1]["text"] == "t2"


def test_ungroup_leaves_a_member_that_points_at_another_lead(with_ungroup):
    group("t1", ["t2", "t3"])
    doc = repository.bound(core).export_document(SLUG)
    next(t for t in doc["tasks"] if t["id"] == "t3")["merged_into"] = "t4"
    repository.bound(core).import_document(SLUG, doc, replace=True)
    state, rejected = ungroup("t1")
    assert rejected == [] and rows(state)["t3"]["merged_into"] == "t4"
    assert state["_meta"]["events"][-1]["text"] == "t2"


def test_ungroup_of_a_task_without_a_group_changes_nothing(with_ungroup):
    state, rejected = ungroup("t1")
    assert rejected == ["ungroup-1"]
    assert state["_meta"]["warnings"][-1] == "tasks/t1 leads no group"
    state, rejected = ungroup("t9", n=2)
    assert rejected == ["ungroup-2"]


@pytest.mark.parametrize(("by", "lane"), [(ENGINEER, "eng"), (PLANNER, "plan")])
def test_a_lane_agent_cannot_ungroup_tasks(with_ungroup, by, lane):
    group("t1", ["t2"])
    state, rejected = ungroup("t1", by=by)
    assert rejected == ["ungroup-1"]
    assert state["_meta"]["warnings"][-1] == f"{by} works in the {lane} lane and cannot release a task group"
    assert rows(state)["t2"]["merged_into"] == "t1"


@pytest.mark.parametrize(
    "op",
    [
        {"op": "task_ungroup", "id": "u", "by": "swarm", "item": "phases/p1"},
        {"op": "task_ungroup", "id": "u", "by": "operator", "item": "tasks/t1"},
        {"op": "task_ungroup", "id": "u", "item": "tasks/t1"},
        {"op": "task_ungroup", "id": "u", "by": "swarm", "item": "tasks/t1", "members": ["t2"]},
    ],
)
def test_a_malformed_ungroup_op_is_refused(op):
    with pytest.raises(ValueError):
        core.check_op(op)


def test_the_ungroup_refusal_names_its_shape():
    with pytest.raises(ValueError) as refused:
        core.check_op({"op": "task_ungroup", "id": "u", "by": "swarm", "item": "tasks/t1", "members": ["t2"]})
    assert str(refused.value) == "task_ungroup takes only an id, by and an item tasks/<lead id>"


def test_the_core_and_the_server_schema_know_the_ungroup_op():
    assert core.EXTENSION_OPS["task_ungroup"] is ledger_groups
    assert ledger_groups.OPS == ("task_group", "task_ungroup")
    schema = schemas.operation_schema("task_ungroup")
    op = {"op": "task_ungroup", "id": "u", "by": "swarm", "item": "tasks/t1"}
    assert schemas.mismatched_field(schema, op) is None
