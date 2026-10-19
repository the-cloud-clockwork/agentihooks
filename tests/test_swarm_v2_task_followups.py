import pytest

from scripts.swarm_v2 import masters
from tests import sv2_mst04_cases as cases
from tests.sv2_mst04_cases import ENGINEER, LEAD, LEAD_NAME, SECOND, SECOND_NAME, SLUG

pytestmark = pytest.mark.unit

ON_TASK = "followups/on-t2"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(cases.core, "LEDGER_DIR", tmp_path / "ledgers")
    cases.core.LEDGER_DIR.mkdir()
    return cases.World()


def add(world, **task):
    op = {"op": "add_item", "id": ON_TASK.partition("/")[2], "by": ENGINEER, "list": "followups"}
    return world.write(op | {"text": "Retry the notes build", **task})


def decide(world, by, path="done", value=True):
    op = {"op": "set", "id": f"set-{by}-{path}", "by": by, "path": f"{ON_TASK}/{path}", "value": value}
    return world.write(op)


def api_checked(world, op):
    from scripts.swarm_ledger.api import schemas

    payload = {"operation_id": f"request-{op['id']}", "ops": [op], "guards": {}}
    return schemas.check_operations(payload, cases.core, tuple(t["id"] for t in world.doc()["tasks"]))


def test_cli_records_the_agents_task_on_a_new_follow_up(monkeypatch):
    from scripts.swarm_ledger import ledger

    captured = []
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t2")
    monkeypatch.setattr(ledger, "send", lambda args, op, **fields: captured.append((op, fields)) or {})
    ledger.cmd_followup(ledger.build_parser().parse_args(["followup", "add", "Retry the notes build"]))
    assert captured == [("add_item", {"list": "followups", "text": "Retry the notes build", "task": "t2"})]


@pytest.mark.parametrize("flags", [[], ["--hive", "h1"], ["--hive", "h1", "--needs-operator"]])
@pytest.mark.parametrize("task", ["t2", None])
def test_the_op_followup_add_builds_passes_the_api_schema(monkeypatch, flags, task):
    from scripts.swarm_ledger import ledger
    from scripts.swarm_ledger.api import schemas

    sent = []
    if task:
        monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", task)
    else:
        monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    monkeypatch.setattr(ledger, "call", lambda slug, ops: sent.extend(ops) or {})
    argv = ["--slug", SLUG, "--as", ENGINEER, "followup", "add", "Retry the notes build", *flags]
    ledger.cmd_followup(ledger.build_parser().parse_args(argv))
    assert [op.get("task") for op in sent] == [task]
    assert [schemas.validate(schemas.operation_schema(op["op"]), op) for op in sent] == [None]


@pytest.mark.parametrize("extra", [{"task": "t2"}, {"hive": "h1"}, {"task": "t2", "hive": "h1"}])
def test_the_add_item_schema_takes_the_task_and_hive_an_agent_sends(extra):
    from scripts.swarm_ledger.api import schemas

    op = {
        "op": "add_item",
        "id": ON_TASK.partition("/")[2],
        "by": ENGINEER,
        "list": "followups",
        "text": "Retry the notes build",
    }
    assert schemas.validate(schemas.operation_schema("add_item"), op | extra) is None


def test_the_add_item_schema_refuses_a_task_that_is_not_text():
    from scripts.swarm_ledger.api import schemas
    from scripts.swarm_ledger.api.errors import APIError

    op = {
        "op": "add_item",
        "id": ON_TASK.partition("/")[2],
        "by": ENGINEER,
        "list": "followups",
        "text": "Retry",
        "task": 2,
    }
    with pytest.raises(APIError) as error:
        schemas.validate(schemas.operation_schema("add_item"), op)
    assert (error.value.status, error.value.code) == (400, "schema_invalid")


def test_cli_adds_no_task_outside_a_task(monkeypatch):
    from scripts.swarm_ledger import ledger

    captured = []
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    monkeypatch.setattr(ledger, "send", lambda args, op, **fields: captured.append(fields) or {})
    ledger.cmd_followup(ledger.build_parser().parse_args(["followup", "add", "Retry the notes build"]))
    assert captured == [{"list": "followups", "text": "Retry the notes build"}]


def test_the_follow_up_row_keeps_its_task(world):
    assert add(world, task="t2")["accepted"] is True
    row = next(f for f in world.doc()["followups"] if f["id"] == "on-t2")
    assert row["task"] == "t2"


@pytest.mark.parametrize("task", ["", "a/b", "a\nb", 7, None, "a" * 129])
def test_a_malformed_task_is_refused(task):
    from scripts.swarm_ledger import ledger_agent_ops

    op = {"op": "add_item", "id": "f", "by": ENGINEER, "list": "followups", "text": "Retry", "task": task}
    with pytest.raises(ValueError) as error:
        ledger_agent_ops.check(op)
    assert str(error.value) == "task needs a task id of up to 128 letters, digits, dots, underscores or hyphens"


@pytest.mark.parametrize("listed", [{"list": "questions"}, {}])
def test_a_task_outside_a_follow_up_is_refused(listed):
    from scripts.swarm_ledger import ledger_agent_ops

    op = {"op": "add_item", "id": "q", "by": ENGINEER, "text": "Which port", "task": "t2"} | listed
    with pytest.raises(ValueError) as error:
        ledger_agent_ops.check(op)
    assert str(error.value) == "task needs a task id of up to 128 letters, digits, dots, underscores or hyphens"


@pytest.mark.parametrize("task", ["t2", "fx-f517f382.code_1", "a" * 128])
def test_a_task_id_on_a_follow_up_passes_the_check(task):
    from scripts.swarm_ledger import ledger_agent_ops

    op = {"op": "add_item", "id": "f", "by": ENGINEER, "list": "followups", "text": "Retry", "task": task}
    ledger_agent_ops.check(op)
    assert op["task"] == task


def test_a_follow_up_add_with_a_task_passes_the_api_schema_and_lands_on_its_owning_master(world):
    op = {"op": "add_item", "id": "on-t2", "by": ENGINEER, "list": "followups", "text": "Retry", "task": "t2"}
    assert api_checked(world, op) == [op]
    assert world.write(op)["accepted"] is True
    doc = world.doc()
    owners = masters.MasterSeats(world.redis).owners(SLUG, doc)
    assert masters.owner_of(ON_TASK, doc, owners) == SECOND


@pytest.mark.parametrize("task", ["", "a/b", "a\nb", 7, None, "a" * 129])
def test_a_malformed_task_is_refused_by_the_api_schema(world, task):
    from scripts.swarm_ledger.api.errors import APIError

    op = {"op": "add_item", "id": "f", "by": ENGINEER, "list": "followups", "text": "Retry", "task": task}
    with pytest.raises(APIError) as error:
        api_checked(world, op)
    assert error.value.code == "schema_invalid"


@pytest.mark.parametrize(
    ("argv", "task", "carried"),
    [
        (["followup", "add", "Retry the notes build"], None, {}),
        (["followup", "add", "Retry the notes build"], "t2", {"task": "t2"}),
        (["followup", "add", "Retry the notes build", "--needs-operator"], None, {"needs_operator": True}),
        (
            ["followup", "add", "Retry the notes build", "--needs-operator"],
            "t2",
            {"needs_operator": True, "task": "t2"},
        ),
        (["followup", "add", "Retry the notes build", "--hive", "h1"], None, {"hive": "h1"}),
        (["followup", "add", "Retry the notes build", "--hive", "h1"], "t2", {"hive": "h1", "task": "t2"}),
        (
            ["followup", "add", "Retry the notes build", "--needs-operator", "--hive", "h1"],
            "t2",
            {"needs_operator": True, "hive": "h1", "task": "t2"},
        ),
        (["question", "add", "Which port"], None, {}),
        (["question", "add", "Which port", "--hive", "h1"], None, {"hive": "h1"}),
    ],
)
def test_every_item_add_the_cli_sends_passes_the_api_schema(world, monkeypatch, argv, task, carried):
    from scripts.swarm_ledger import ledger

    sent = []
    if task:
        monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", task)
    else:
        monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    monkeypatch.setattr(ledger, "call", lambda slug, ops, *rest: sent.extend(ops) or {})
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", ENGINEER, *argv])
    {"followup": ledger.cmd_followup, "question": ledger.cmd_question}[args.command](args)
    assert [op["op"] for op in sent] == ["add_item"]
    assert {k: v for k, v in sent[0].items() if k in ("task", "hive", "needs_operator")} == carried
    assert api_checked(world, sent[0]) == sent


def test_a_follow_up_on_a_task_belongs_to_the_tasks_owning_master(world):
    add(world, task="t2")
    doc = world.doc()
    table = masters.MasterSeats(world.redis)

    def owners():
        return table.owners(SLUG, doc)

    assert masters.owner_of(ON_TASK, doc, owners()) == SECOND
    assert masters.route(ON_TASK, doc, owners, [LEAD, SECOND], LEAD) == SECOND


def test_a_follow_up_with_no_task_stays_with_the_front_door(world):
    add(world)
    doc = world.doc()
    table = masters.MasterSeats(world.redis)

    def owners():
        return table.owners(SLUG, doc)

    assert masters.owner_of(ON_TASK, doc, owners()) == ""
    assert masters.route(ON_TASK, doc, owners, [LEAD, SECOND], LEAD) == LEAD


def test_the_owning_master_decides_flags_and_comments_on_it(world):
    add(world, task="t2")

    assert decide(world, SECOND_NAME, "needs_operator") == {"accepted": True, "warnings": []}
    assert decide(world, SECOND_NAME) == {"accepted": True, "warnings": []}
    comment = {"op": "add", "thread": f"{ON_TASK}/comments", "id": "c-second", "by": SECOND_NAME}
    assert world.write(comment | {"text": "Turned into a task"})["accepted"] is True
    row = next(f for f in world.doc()["followups"] if f["id"] == "on-t2")
    assert row["done"] is True
    assert row["needs_operator"] is True
    assert world.refusals() == 0


@pytest.mark.parametrize("path", ["done", "needs_operator"])
def test_another_master_is_refused_on_it(world, path):
    add(world, task="t2")

    assert decide(world, LEAD_NAME, path)["warnings"] == [f"{ON_TASK} belongs to {SECOND}, so {LEAD} cannot decide it"]
    assert world.refusals() == 1


def test_its_notice_goes_to_the_tasks_owning_master(world):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events

    inbox = InboxStore(world.redis)
    ledger_events.event_pass(inbox, world.store, SLUG, world.doc(), None, 0)
    add(world, task="t2")
    ledger_events.event_pass(inbox, world.store, SLUG, world.doc(), None, 0)
    ref = f"{SLUG}:event:{ON_TASK}"
    assert [item.address for item in inbox.mailbox(SECOND_NAME) if item.ref == ref] == [SECOND]
    assert [item for item in inbox.mailbox(LEAD_NAME) if item.ref == ref] == []


def test_its_raised_notice_goes_to_the_tasks_owning_master(world):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events

    inbox = InboxStore(world.redis)
    ledger_events.event_pass(inbox, world.store, SLUG, world.doc(), None, 0)
    add(world, task="t2")
    doc = world.doc()
    added = next(e for e in doc["_meta"]["events"] if e["target"] == ON_TASK)
    ledger_events.event_pass(inbox, world.store, SLUG, doc, None, added["at"] + ledger_events.FOLLOWUP_RAISE_MS)
    raised = "A follow-up on-t2 on ledger"
    assert [item.address for item in inbox.mailbox(SECOND_NAME) if raised in item.text] == [SECOND]
    assert [item for item in inbox.mailbox(LEAD_NAME) if raised in item.text] == []
