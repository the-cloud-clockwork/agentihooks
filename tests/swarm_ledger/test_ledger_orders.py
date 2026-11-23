import itertools
import json

import pytest

from hooks.context import operator_words
from scripts.swarm_ledger import ledger, ledger_tasks, new_ledger
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger.api import schemas
from tests.swarm_ledger import legacy_page

pytestmark = pytest.mark.unit

SLUG = "order-marks"
IDS = itertools.count()
MASTER, ENGINEER = "master@c0ffee-0001", "engineer@c0ffee-0002"
SAID = "Build the reviewer seats today before anything else runs"
QUOTE = "build the  reviewer seats TODAY"
HEARD = "Build the reviewer seats today"
UNVERIFIED = "the server could not verify at least three quoted operator words in its recorded prompts or answers"
NOT_HIS = "order refused: the quote is not in an operator prompt or answer any master or planner of this swarm recorded"
USAGE = "order takes ITEM with --quote WORDS or --request ENTRY, and --clear only with --quote WORDS"
NO_AUTHOR = "order ops take by, the relaying agent's name, or none for the operator"
NO_ITEM = "an order names phases/<id> or tasks/<id>"
NO_PROOF = "an agent's order needs quote, the operator's words, or request, the id of an entry he wrote"
NO_TEXT = "must be text up to 20000 characters"
NOT_ADDED = "this call did not add the entry"
NO_ORCHESTRATOR = "is not an orchestrator of this ledger"
HIS = ("--quote", QUOTE)


@pytest.fixture(autouse=True)
def order_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    for name in ledger_tasks.OPS:
        monkeypatch.setitem(core.EXTENSION_OPS, name, ledger_tasks)
    content = {"title": "Orders", "overview": "Intent", "sources": [], "phases": [{"title": "Build"}]}
    html, json_path = legacy_page.paths(SLUG, core)
    json_path.unlink(missing_ok=True)
    html.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765))
    core.sync(SLUG)
    task = {"task": "t1", "title": "Build it", "lane": "eng", "phase": "p1"}
    assert write("task_add", by="planner", **task)[1] == []
    assert write("join", by=MASTER, role="orchestrator")[1] == []
    assert write("join", by=ENGINEER)[1] == []
    operator_words.record(MASTER, SAID)


def write(kind, by=None, **fields):
    op = {"op": kind, "id": f"{kind}-{next(IDS)}", **fields}
    if by is not None:
        op["by"] = by
    core.check_op(op)
    return core.sync(SLUG, ops=[op])


def order(item, by: str | None = MASTER, **fields):
    return write("order_set", by=by, item=item, **fields)


def row(state, item):
    name, item_id = item.split("/")
    return next(found for found in state[name] if found["id"] == item_id)


def event_of(state, kind):
    events = reversed(state["_meta"]["events"])
    event = next(found for found in events if found.get("id", "").startswith(f"{kind}-"))
    return {key: value for key, value in event.items() if key not in ("rev", "at", "id")}


def refusals(state):
    return [warning for warning in state["_meta"]["warnings"] if warning.startswith("order on ")]


def unmarked(state, item):
    events = [event for event in state["_meta"]["events"] if event["kind"] in ("ordered", "order cleared")]
    return "ordered" not in row(state, item) and not events and f"{item}/ordered" not in state["_meta"]["stamps"]


def cli(monkeypatch, *argv, by=MASTER):
    def call(slug, ops=None):
        state, rejected = core.sync(slug, ops=ops)
        return {**state, "rejected": rejected, "applied": [op["id"] for op in ops or [] if op["id"] not in rejected]}

    monkeypatch.setattr(ledger, "call", call)
    monkeypatch.setattr("sys.argv", ["ledger", "--slug", SLUG, "--as", by, *argv])
    ledger.main()


def spied(monkeypatch):
    calls = []
    real = core.sync
    monkeypatch.setattr(core, "sync", lambda slug, ops=None, **more: calls.append(ops) or real(slug, ops=ops, **more))
    return calls, real


@pytest.mark.feature("ledger-order-mark")
@pytest.mark.parametrize("item", ["phases/p1", "tasks/t1"])
def test_the_orchestrator_marks_an_item_with_the_operators_words_as_he_wrote_them(item):
    state, rejected = order(item, quote=QUOTE)
    assert rejected == []
    mark = row(state, item)["ordered"]
    assert mark == {"at": mark["at"], "by": MASTER, "quote": HEARD}
    assert mark["at"] > 0
    marks = {"relayed_by": MASTER, "relayed_from": "master pane", "quote": HEARD}
    assert event_of(state, "order_set") == {"by": "operator", "kind": "ordered", "target": item} | marks
    assert state["_meta"]["stamps"][f"{item}/ordered"]["by"] == "operator"


@pytest.mark.feature("ledger-order-mark")
def test_a_ledger_entry_the_operator_wrote_marks_the_item_it_asks_for():
    asked = write("add", thread="chat", text="Build the reviewer seats")[0]["chat"][-1]["id"]
    state, rejected = order("phases/p1", request=asked)
    assert rejected == []
    mark = row(state, "phases/p1")["ordered"]
    assert mark == {"at": mark["at"], "by": MASTER, "request": asked}
    marks = {"relayed_by": MASTER, "relayed_from": "master pane", "request": asked}
    assert event_of(state, "order_set") == {"by": "operator", "kind": "ordered", "target": "phases/p1"} | marks


@pytest.mark.feature("ledger-order-mark")
def test_an_entry_an_agent_wrote_orders_nothing():
    write("add", by=ENGINEER, thread="phases/p1/comments", text="Started the build")
    comment = core.sync(SLUG)[0]["phases"][0]["comments"][-1]["id"]
    for request in (comment, "no-such-entry"):
        state, rejected = order("phases/p1", request=request)
        assert len(rejected) == 1
        assert unmarked(state, "phases/p1")
        assert refusals(state) == [f"order on phases/p1 refused: the entry {request} is not one the operator wrote"]


@pytest.mark.feature("ledger-order-mark")
@pytest.mark.parametrize("quote", ["ship the warm writer first", "reviewer seats"])
def test_words_the_operator_never_said_order_nothing(quote):
    state, rejected = order("tasks/t1", quote=quote)
    assert len(rejected) == 1
    assert unmarked(state, "tasks/t1")
    assert refusals(state) == [f"order on tasks/t1 refused: {UNVERIFIED}"]


@pytest.mark.feature("ledger-order-mark")
@pytest.mark.parametrize("proof", [{"quote": QUOTE}, {"request": "no-such-entry"}])
def test_only_the_orchestrator_relays_an_order(proof):
    operator_words.record(ENGINEER, SAID)
    state, rejected = order("phases/p1", by=ENGINEER, **proof)
    assert len(rejected) == 1
    assert unmarked(state, "phases/p1")
    assert refusals(state) == [f"order on phases/p1 refused: {ENGINEER} {NO_ORCHESTRATOR}"]


@pytest.mark.feature("ledger-order-mark")
@pytest.mark.parametrize("item", ["phases/p9", "tasks/t9"])
def test_an_order_on_an_item_the_ledger_does_not_hold_is_refused(item):
    state, rejected = order(item, quote=QUOTE)
    assert len(rejected) == 1
    assert refusals(state) == [f"order on {item} refused: the entry does not exist"]


@pytest.mark.feature("ledger-order-mark")
def test_the_operator_marks_an_item_himself():
    state, rejected = order("tasks/t1", by=None)
    assert rejected == []
    mark = row(state, "tasks/t1")["ordered"]
    assert mark == {"at": mark["at"], "by": "operator"}
    assert event_of(state, "order_set") == {"by": "operator", "kind": "ordered", "target": "tasks/t1"}


@pytest.mark.feature("ledger-order-mark")
def test_the_operators_own_order_keeps_the_words_he_gives_unchecked():
    state, rejected = order("phases/p1", by=None, quote="ship the warm writer first")
    assert rejected == []
    mark = row(state, "phases/p1")["ordered"]
    assert mark == {"at": mark["at"], "by": "operator", "quote": "ship the warm writer first"}


@pytest.mark.feature("ledger-order-mark")
def test_a_second_order_keeps_the_first_mark_and_still_needs_its_proof():
    first = row(order("phases/p1", quote=QUOTE)[0], "phases/p1")["ordered"]
    events = len(core.sync(SLUG)[0]["_meta"]["events"])
    state, rejected = order("phases/p1", by=None)
    assert rejected == []
    assert row(state, "phases/p1")["ordered"] == first
    assert len(state["_meta"]["events"]) == events
    state, rejected = order("phases/p1", quote="ship the warm writer first")
    assert len(rejected) == 1
    assert refusals(state) == [f"order on phases/p1 refused: {UNVERIFIED}"]
    assert row(state, "phases/p1")["ordered"] == first


@pytest.mark.feature("ledger-order-mark")
def test_only_the_operator_or_his_words_clear_an_order():
    order("phases/p1", quote=QUOTE)
    state, rejected = write("order_clear", by=ENGINEER, item="phases/p1", quote=QUOTE)
    assert len(rejected) == 1
    assert refusals(state) == [f"order on phases/p1 refused: {ENGINEER} {NO_ORCHESTRATOR}"]
    state, rejected = write("order_clear", by=MASTER, item="phases/p1", quote="drop the reviewer seats")
    assert len(rejected) == 1
    assert refusals(state) == [f"order on phases/p1 refused: {UNVERIFIED}"]
    assert "ordered" in row(state, "phases/p1")
    state, rejected = write("order_clear", by=MASTER, item="phases/p1", quote="reviewer seats today before")
    assert rejected == []
    assert "ordered" not in row(state, "phases/p1")
    marks = {"relayed_by": MASTER, "relayed_from": "master pane", "quote": "reviewer seats today before"}
    cleared = {"by": "operator", "kind": "order cleared", "target": "phases/p1"}
    assert event_of(state, "order_clear") == cleared | marks
    assert state["_meta"]["stamps"]["phases/p1/ordered"]["rev"] == state["_meta"]["rev"]
    order("phases/p1", by=None)
    state, rejected = write("order_clear", item="phases/p1")
    assert rejected == []
    assert "ordered" not in row(state, "phases/p1")
    assert event_of(state, "order_clear") == cleared


@pytest.mark.feature("ledger-order-mark")
def test_clearing_an_item_that_carries_no_order_changes_nothing_and_still_needs_its_proof():
    events = len(core.sync(SLUG)[0]["_meta"]["events"])
    state, rejected = write("order_clear", item="tasks/t1")
    assert rejected == []
    assert len(state["_meta"]["events"]) == events
    state, rejected = write("order_clear", by=ENGINEER, item="tasks/t1", quote=QUOTE)
    assert len(rejected) == 1
    assert refusals(state) == [f"order on tasks/t1 refused: {ENGINEER} {NO_ORCHESTRATOR}"]
    state, rejected = write("order_clear", by=MASTER, item="tasks/t1", quote="ship the warm writer first")
    assert len(rejected) == 1
    assert refusals(state) == [f"order on tasks/t1 refused: {UNVERIFIED}"]


@pytest.mark.feature("ledger-order-mark")
def test_updates_a_split_and_a_group_keep_the_order():
    for name, title in (("g1", "Wire the reviewer panes"), ("g2", "Record the seat costs")):
        added = {"task": name, "title": title, "lane": "eng", "phase": "p1", "difficulty": "S"}
        assert write("task_add", by=MASTER, **added)[1] == []
    marked = ("phases/p1", "tasks/t1", "tasks/g1", "tasks/g2")
    for item in marked:
        assert order(item, quote=QUOTE)[1] == []
    assert write("phase_update", by=MASTER, item="phases/p1", fields={"title": "Build the seats"})[1] == []
    assert write("task_update", by=MASTER, item="tasks/t1", fields={"description": "Seats first"})[1] == []
    assert write("task_split", by=MASTER, item="tasks/t1", halves=["first", "second"])[1] == []
    state, rejected = write("task_group", by=MASTER, item="tasks/g1", members=["g2"])
    assert rejected == []
    assert row(state, "phases/p1")["title"] == "Build the seats"
    assert (row(state, "tasks/t1")["description"], row(state, "tasks/t1")["split"]) == (
        "Seats first",
        ["first", "second"],
    )
    assert row(state, "tasks/g2")["merged_into"] == "g1"
    assert [row(state, item)["ordered"]["quote"] for item in marked] == [HEARD] * 4


@pytest.mark.feature("ledger-order-mark")
@pytest.mark.parametrize(
    ("op", "message"),
    [
        ({"op": "order_set", "by": MASTER, "item": "followups/f1", "quote": QUOTE}, NO_ITEM),
        ({"op": "order_set", "by": MASTER, "item": "phases/p1/comments", "quote": QUOTE}, NO_ITEM),
        ({"op": "order_set", "by": MASTER, "item": "tasks/", "quote": QUOTE}, NO_ITEM),
        ({"op": "order_set", "by": MASTER, "quote": QUOTE}, NO_ITEM),
        ({"op": "order_set", "by": MASTER, "item": "phases/p1"}, NO_PROOF),
        ({"op": "order_set", "by": MASTER, "item": "phases/p1", "filing": True}, NO_PROOF),
        (
            {"op": "order_set", "by": MASTER, "item": "phases/p1", "quote": QUOTE, "request": "c1"},
            "an order takes quote or request, not both",
        ),
        (
            {"op": "order_set", "item": "phases/p1", "quote": QUOTE, "request": "c1"},
            "an order takes quote or request, not both",
        ),
        ({"op": "order_set", "by": MASTER, "item": "phases/p1", "quote": "  "}, f"quote {NO_TEXT}"),
        ({"op": "order_set", "by": MASTER, "item": "phases/p1", "quote": 7}, f"quote {NO_TEXT}"),
        ({"op": "order_set", "by": MASTER, "item": "phases/p1", "request": ""}, f"request {NO_TEXT}"),
        ({"op": "order_set", "by": MASTER, "item": "phases/p1", "quote": "x" * 20001}, f"quote {NO_TEXT}"),
        ({"op": "order_set", "by": "operator", "item": "phases/p1", "quote": QUOTE}, NO_AUTHOR),
        ({"op": "order_set", "by": 7, "item": "phases/p1", "quote": QUOTE}, NO_AUTHOR),
        ({"op": "order_set", "by": "no name", "item": "phases/p1", "quote": QUOTE}, NO_AUTHOR),
        (
            {"op": "order_set", "by": MASTER, "item": "phases/p1", "quote": QUOTE, "rank": "urgent"},
            "order_set takes only item quote request filing",
        ),
        (
            {"op": "order_set", "by": MASTER, "item": "phases/p1", "quote": QUOTE, "filing": False},
            "filing is true on an order sent with the add of its item",
        ),
        (
            {"op": "order_set", "by": MASTER, "item": "phases/p1", "quote": QUOTE, "filing": "yes"},
            "filing is true on an order sent with the add of its item",
        ),
        (
            {"op": "order_clear", "by": MASTER, "item": "phases/p1"},
            "an agent clears an order only with quote, the operator's words",
        ),
        (
            {"op": "order_clear", "by": MASTER, "item": "phases/p1", "request": "c1"},
            "order_clear takes only item quote",
        ),
        (
            {"op": "order_clear", "by": MASTER, "item": "phases/p1", "quote": QUOTE, "filing": True},
            "order_clear takes only item quote",
        ),
    ],
)
def test_a_malformed_order_is_refused_at_check(op, message):
    with pytest.raises(ValueError) as refused:
        core.check_op({"id": "o1", **op})
    assert str(refused.value) == message


@pytest.mark.feature("ledger-order-mark")
def test_a_well_formed_order_passes_check():
    for op in (
        {"op": "order_set", "item": "phases/p1"},
        {"op": "order_set", "item": "phases/p1", "request": "c1"},
        {"op": "order_set", "by": MASTER, "item": "tasks/t1", "quote": "x" * 20000},
        {"op": "order_set", "by": MASTER, "item": "tasks/t1", "request": "c1"},
        {"op": "order_set", "by": MASTER, "item": "tasks/t1", "quote": QUOTE, "filing": True},
        {"op": "order_clear", "item": "tasks/t1"},
        {"op": "order_clear", "by": MASTER, "item": "tasks/t1", "quote": QUOTE},
    ):
        core.check_op({"id": "o1", **op})


@pytest.mark.feature("ledger-order-mark")
def test_order_ops_are_in_the_api_contract_and_target_their_item():
    assert schemas.FIELDS["order_set"] == "by item quote request filing"
    assert schemas.FIELDS["order_clear"] == "by item quote"
    assert schemas.operation_schema("order_set")["properties"]["filing"] == {"const": True}
    for kind in ("order_set", "order_clear"):
        assert schemas.target({"op": kind, "id": "o1", "item": "phases/p1"}) == "phases/p1"


@pytest.mark.feature("ledger-order-mark")
def test_the_order_command_marks_and_clears_through_the_cli(monkeypatch, capsys):
    cli(monkeypatch, "order", "tasks/t1", "--quote", QUOTE)
    assert json.loads(capsys.readouterr().out) == {"ordered": "tasks/t1"}
    assert row(core.sync(SLUG)[0], "tasks/t1")["ordered"]["quote"] == HEARD
    cli(monkeypatch, "order", "tasks/t1", "--clear", "--quote", QUOTE)
    assert json.loads(capsys.readouterr().out) == {"cleared": "tasks/t1"}
    assert "ordered" not in row(core.sync(SLUG)[0], "tasks/t1")


@pytest.mark.feature("ledger-order-mark")
def test_the_ledger_entry_point_runs_an_order_the_outbox_holds_as_queued(monkeypatch, capsys):
    monkeypatch.setattr(ledger, "call", lambda slug, ops: {"queued": ops[0]["id"]})
    monkeypatch.setattr("sys.argv", ["ledger", "--slug", SLUG, "--as", MASTER, "order", "tasks/t1", "--quote", QUOTE])
    ledger.main()
    assert json.loads(capsys.readouterr().out) == {"ordered": "tasks/t1", "queued": True}


@pytest.mark.feature("ledger-order-mark")
def test_the_order_command_takes_a_request_and_refuses_words_he_never_said(monkeypatch, capsys):
    asked = write("add", thread="chat", text="Build the reviewer seats")[0]["chat"][-1]["id"]
    cli(monkeypatch, "order", "phases/p1", "--request", asked)
    assert json.loads(capsys.readouterr().out) == {"ordered": "phases/p1"}
    assert row(core.sync(SLUG)[0], "phases/p1")["ordered"]["request"] == asked
    for clear in ([], ["--clear"]):
        with pytest.raises(SystemExit) as refused:
            cli(monkeypatch, "order", "tasks/t1", *clear, "--quote", "ship the warm writer first")
        assert refused.value.code == NOT_HIS
    assert "ordered" not in row(core.sync(SLUG)[0], "tasks/t1")
    assert "ordered" in row(core.sync(SLUG)[0], "phases/p1")


@pytest.mark.feature("ledger-order-mark")
@pytest.mark.parametrize(
    "argv",
    [
        ["order", "tasks/t1"],
        ["order", "tasks/t1", "--quote", QUOTE, "--request", "c1"],
        ["order", "tasks/t1", "--clear", "--request", "c1"],
    ],
)
def test_the_order_command_needs_exactly_one_proof_and_clears_only_on_his_words(monkeypatch, argv):
    with pytest.raises(SystemExit) as refused:
        cli(monkeypatch, *argv)
    assert refused.value.code == USAGE


@pytest.mark.feature("ledger-order-mark")
@pytest.mark.parametrize(
    ("argv", "text"),
    [
        (["--help"], "order mark phases/<id> or tasks/<id> as the operator's order"),
        (["order", "--help"], "--quote QUOTE the operator's recorded words"),
        (["order", "--help"], "--request REQUEST id of the operator chat line or comment that ordered the work"),
        (["order", "--help"], "--clear remove the mark, with --quote"),
    ],
)
def test_the_help_names_the_order_command_and_its_flags(capsys, argv, text):
    with pytest.raises(SystemExit):
        ledger.build_parser().parse_args(argv)
    assert text in " ".join(capsys.readouterr().out.split())


@pytest.mark.feature("ledger-order-at-filing")
def test_phase_add_with_his_words_files_and_orders_the_phase_in_one_call(monkeypatch, capsys):
    calls, real = spied(monkeypatch)
    cli(monkeypatch, "phase", "add", "p7", "Reviewer", "seats", "--description", "Seats", "--quote", QUOTE)
    assert [[op["op"] for op in ops] for ops in calls if ops] == [["phase_add", "order_set"]]
    state = real(SLUG)[0]
    assert row(state, "phases/p7")["title"] == "Reviewer seats"
    mark = row(state, "phases/p7")["ordered"]
    assert mark == {"at": mark["at"], "by": MASTER, "quote": HEARD}
    printed = capsys.readouterr()
    assert json.loads(printed.out)["title"] == "Reviewer seats"
    assert ("not_ordered" in json.loads(printed.out), printed.err) == (False, "")


@pytest.mark.feature("ledger-order-at-filing")
def test_task_add_with_his_words_files_and_orders_the_task_in_one_call(monkeypatch, capsys):
    calls, real = spied(monkeypatch)
    cli(monkeypatch, "task", "add", "t7", "Fix", "the", "writer", "--phase", "p1", "--quote", QUOTE)
    assert [[op["op"] for op in ops] for ops in calls if ops] == [["task_add", "order_set"]]
    state = real(SLUG)[0]
    assert row(state, "tasks/t7")["title"] == "Fix the writer"
    assert row(state, "tasks/t7")["ordered"]["quote"] == HEARD
    assert json.loads(capsys.readouterr().out) == {"task": "t7", "added": "Fix the writer"}


@pytest.mark.feature("ledger-order-at-filing")
@pytest.mark.parametrize(
    "argv",
    [
        ["phase", "add", "p7", "Reviewer seats", "--quote", "ship the warm writer first"],
        ["task", "add", "t7", "Fix the writer", "--phase", "p1", "--quote", "ship the warm writer first"],
    ],
)
def test_filing_with_words_he_never_said_writes_nothing(monkeypatch, argv):
    with pytest.raises(SystemExit) as refused:
        cli(monkeypatch, *argv)
    assert refused.value.code == NOT_HIS
    state = core.sync(SLUG)[0]
    assert [phase["id"] for phase in state["phases"]] == ["p1"]
    assert [task["id"] for task in state["tasks"]] == ["t1"]


@pytest.mark.feature("ledger-order-at-filing")
@pytest.mark.parametrize(
    ("argv", "item"),
    [
        (["task", "add", "t7", "Fix the writer", "--phase", "p1"], "tasks/t7"),
        (["phase", "add", "p7", "Reviewer seats"], "phases/p7"),
    ],
)
def test_an_add_whose_order_the_server_refuses_is_filed_and_says_it_is_not_ordered(monkeypatch, capsys, argv, item):
    operator_words.record("planner", SAID)
    cli(monkeypatch, *argv, "--quote", QUOTE, by="planner")
    reason = f"order on {item} refused: planner {NO_ORCHESTRATOR}"
    printed = capsys.readouterr()
    assert json.loads(printed.out)["not_ordered"] == reason
    assert printed.err == f"not ordered: {reason}\n"
    assert "ordered" not in row(core.sync(SLUG)[0], item)


@pytest.mark.feature("ledger-order-at-filing")
def test_words_the_server_cannot_verify_leave_the_add_filed_and_not_ordered(monkeypatch, capsys):
    monkeypatch.setattr(ledger, "his_words", lambda args, what: None)
    cli(monkeypatch, "task", "add", "t7", "Fix the writer", "--phase", "p1", "--quote", "ship the warm writer first")
    reason = f"order on tasks/t7 refused: {UNVERIFIED}"
    printed = capsys.readouterr()
    assert json.loads(printed.out) == {"task": "t7", "added": "Fix the writer", "not_ordered": reason}
    assert printed.err == f"not ordered: {reason}\n"
    assert unmarked(core.sync(SLUG)[0], "tasks/t7")


@pytest.mark.feature("ledger-order-at-filing")
def test_an_order_refused_with_no_reason_from_the_server_still_says_it_is_not_ordered(monkeypatch, capsys):
    monkeypatch.setattr(ledger, "call", lambda slug, ops: {"rejected": [ops[1]["id"]], "applied": [ops[0]["id"]]})
    monkeypatch.setattr("sys.argv", ["ledger", "--slug", SLUG, "--as", MASTER, "phase", "add", "p7", "Seats", *HIS])
    ledger.main()
    reason = (
        "order_set on phases/p7 refused without a reason from the server: "
        "check that the entry exists, that you may change it and that its text is not empty"
    )
    printed = capsys.readouterr()
    assert json.loads(printed.out)["not_ordered"] == reason
    assert printed.err == f"not ordered: {reason}\n"


@pytest.mark.feature("ledger-order-at-filing")
def test_a_scaffolded_task_whose_order_is_refused_still_gets_its_work_folder(monkeypatch, capsys):
    made = []

    def full(slug, task, doc=None):
        raise OSError("full")

    monkeypatch.setattr(ledger.ledger_workspace, "scaffold", lambda slug, task, doc: made.append(task["id"]))
    operator_words.record("planner", SAID)
    cli(monkeypatch, "task", "add", "t7", "Fix the writer", "--phase", "p1", "--scaffold", *HIS, by="planner")
    assert made == ["t7"]
    assert json.loads(capsys.readouterr().out)["not_ordered"] == f"order on tasks/t7 refused: planner {NO_ORCHESTRATOR}"
    monkeypatch.setattr(ledger.ledger_workspace, "scaffold", full)
    cli(monkeypatch, "task", "add", "t8", "Mend the reader", "--phase", "p1", "--scaffold", *HIS, by="planner")
    printed = json.loads(capsys.readouterr().out)
    refused = f"order on tasks/t8 refused: planner {NO_ORCHESTRATOR}"
    assert (printed["workspace_error"], printed["not_ordered"]) == ("full", refused)


@pytest.mark.feature("ledger-order-at-filing")
def test_an_add_that_files_nothing_new_leaves_the_entry_it_names_unordered(monkeypatch, capsys):
    with pytest.raises(SystemExit) as refused:
        cli(monkeypatch, "task", "add", "t1", "Build it again", "--phase", "p1", "--quote", QUOTE)
    assert str(refused.value.code).startswith('task t1 was not added: task t1 already exists as "Build it" (open)')
    assert str(refused.value.code).endswith(f"; order on tasks/t1 refused: {NOT_ADDED}")
    capsys.readouterr()
    cli(monkeypatch, "phase", "add", "p1", "Build again", "--quote", QUOTE)
    printed = capsys.readouterr()
    assert json.loads(printed.out)["not_ordered"] == f"order on phases/p1 refused: {NOT_ADDED}"
    state = core.sync(SLUG)[0]
    assert unmarked(state, "tasks/t1") and unmarked(state, "phases/p1")
    assert row(state, "phases/p1")["title"] == "Build"


@pytest.mark.feature("ledger-order-at-filing")
def test_the_not_ordered_reason_is_the_servers_refusal_of_that_order(monkeypatch, capsys):
    said = ["task t9 depends on unknown tasks: t8", "order on phases/p8 refused: no", "order on phases/p7 refused: yes"]

    def call(slug, ops):
        return {"rejected": [ops[1]["id"]], "applied": [ops[0]["id"]], "_meta": {"warnings": said}}

    monkeypatch.setattr(ledger, "call", call)
    monkeypatch.setattr("sys.argv", ["ledger", "--slug", SLUG, "--as", MASTER, "phase", "add", "p7", "Seats", *HIS])
    ledger.main()
    printed = capsys.readouterr()
    assert json.loads(printed.out)["not_ordered"] == "order on phases/p7 refused: yes"
    assert printed.err == "not ordered: order on phases/p7 refused: yes\n"


@pytest.mark.feature("ledger-order-at-filing")
def test_an_order_sent_as_a_filing_marks_only_the_entry_its_own_call_added():
    state, rejected = order("tasks/t1", quote=QUOTE, filing=True)
    assert len(rejected) == 1
    assert refusals(state) == [f"order on tasks/t1 refused: {NOT_ADDED}"]
    assert unmarked(state, "tasks/t1")
    added = {"op": "task_add", "id": "add-t7", "by": MASTER, "task": "t7", "title": "Fix the writer"}
    filed = {"op": "order_set", "id": "order-t1", "by": MASTER, "item": "tasks/t1", "quote": QUOTE, "filing": True}
    state, rejected = core.sync(SLUG, ops=[{**added, "lane": "eng", "phase": "p1"}, filed])
    assert rejected == ["order-t1"]
    assert refusals(state) == [f"order on tasks/t1 refused: {NOT_ADDED}"]
    assert unmarked(state, "tasks/t1")
    assert row(state, "tasks/t7")["title"] == "Fix the writer"


@pytest.mark.feature("ledger-order-at-filing")
def test_a_task_the_server_refuses_to_add_still_reads_as_not_added(monkeypatch):
    operator_words.record(ENGINEER, SAID)
    with pytest.raises(SystemExit) as refused:
        cli(monkeypatch, "task", "add", "t7", "Fix the writer", "--phase", "p1", "--quote", QUOTE, by=ENGINEER)
    assert str(refused.value.code).startswith(f"task t7 was not added: {ENGINEER} works in the eng lane")
    assert [task["id"] for task in core.sync(SLUG)[0]["tasks"]] == ["t1"]


@pytest.mark.feature("ledger-order-at-filing")
def test_filing_without_his_words_sends_the_add_alone(monkeypatch, capsys):
    calls, real = spied(monkeypatch)
    cli(monkeypatch, "phase", "add", "p7", "Reviewer seats")
    cli(monkeypatch, "task", "add", "t7", "Fix the writer", "--phase", "p1")
    assert [[op["op"] for op in ops] for ops in calls if ops] == [["phase_add"], ["task_add"]]
    state = real(SLUG)[0]
    assert "ordered" not in row(state, "phases/p7")
    assert "ordered" not in row(state, "tasks/t7")
    capsys.readouterr()


@pytest.mark.feature("ledger-order-at-filing")
def test_a_filing_the_outbox_holds_prints_what_it_filed(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(ledger, "call", lambda slug, ops: sent.extend(ops) or {"queued": ops[0]["id"]})
    args = ["--slug", SLUG, "--as", MASTER, "phase", "add", "p7", "Reviewer seats", "--quote", QUOTE]
    ledger.cmd_phase(ledger.build_parser().parse_args(args))
    sent_as = [(op["op"], op.get("item"), op.get("filing")) for op in sent]
    assert sent_as == [("phase_add", None, None), ("order_set", "phases/p7", True)]
    assert json.loads(capsys.readouterr().out)["title"] == "Reviewer seats"


@pytest.mark.feature("ledger-task-add-queued")
@pytest.mark.parametrize(
    ("words", "sent_as"),
    [
        ((), [("task_add", None)]),
        (HIS, [("task_add", None), ("order_set", "tasks/t7")]),
    ],
)
def test_a_task_add_the_outbox_holds_says_it_is_queued(monkeypatch, capsys, words, sent_as):
    sent = []
    monkeypatch.setattr(ledger, "call", lambda slug, ops: sent.extend(ops) or {"queued": ops[0]["id"]})
    argv = ["--slug", SLUG, "--as", MASTER, "task", "add", "t7", "Fix the writer", "--phase", "p1", *words]
    ledger.cmd_task(ledger.build_parser().parse_args(argv))
    assert [(op["op"], op.get("item")) for op in sent] == sent_as
    assert json.loads(capsys.readouterr().out) == {"task": "t7", "added": "Fix the writer", "queued": True}


@pytest.mark.feature("ledger-task-add-queued")
def test_a_scaffolded_task_add_the_outbox_holds_still_gets_its_work_folder(monkeypatch, capsys):
    made = []
    monkeypatch.setattr(ledger, "call", lambda slug, ops: {"queued": ops[0]["id"]})
    monkeypatch.setattr(ledger.ledger_workspace, "scaffold", lambda slug, task, doc: made.append(task["id"]))
    argv = ["--slug", SLUG, "--as", MASTER, "task", "add", "t7", "Fix the writer", "--phase", "p1", "--scaffold"]
    ledger.cmd_task(ledger.build_parser().parse_args(argv))
    assert made == ["t7"]
    assert json.loads(capsys.readouterr().out) == {"task": "t7", "added": "Fix the writer", "queued": True}


@pytest.mark.feature("ledger-task-add-queued")
@pytest.mark.parametrize("words", [(), HIS])
def test_a_phase_add_the_outbox_holds_says_it_is_queued(monkeypatch, capsys, words):
    monkeypatch.setattr(ledger, "call", lambda slug, ops: {"queued": ops[0]["id"]})
    argv = ["--slug", SLUG, "--as", MASTER, "phase", "add", "p7", "Reviewer seats", *words]
    ledger.cmd_phase(ledger.build_parser().parse_args(argv))
    printed = json.loads(capsys.readouterr().out)
    assert (printed["title"], printed["queued"]) == ("Reviewer seats", True)


@pytest.mark.feature("ledger-order-at-filing")
@pytest.mark.parametrize(
    ("command", "text"),
    [
        ("phase", "--quote QUOTE phase add: the operator's words, filing the phase as his order"),
        (
            "task",
            "--quote QUOTE task set rank=...: the operator's words, so the rank stays his, "
            "and task add: his words, filing the task as his order",
        ),
    ],
)
def test_the_help_names_the_quote_that_files_an_order(capsys, command, text):
    with pytest.raises(SystemExit):
        ledger.build_parser().parse_args([command, "--help"])
    assert text in " ".join(capsys.readouterr().out.split())


@pytest.mark.feature("ledger-order-at-filing")
@pytest.mark.parametrize("argv", [["phase", "set", "p1", "title=Seats"], ["phase", "p1", "done"]])
def test_only_phase_add_takes_his_words(monkeypatch, argv):
    with pytest.raises(SystemExit) as refused:
        cli(monkeypatch, *argv, "--quote", QUOTE)
    assert refused.value.code == "phase takes --quote only with add: run order phases/<id> --quote <his words>"
    state = core.sync(SLUG)[0]
    assert (row(state, "phases/p1")["title"], row(state, "phases/p1")["done"]) == ("Build", False)
    assert "ordered" not in row(state, "phases/p1")


@pytest.mark.feature("ledger-order-at-filing")
@pytest.mark.parametrize("action", ["group", "split"])
def test_the_other_task_actions_still_take_no_quote(monkeypatch, action):
    with pytest.raises(SystemExit) as refused:
        cli(monkeypatch, "task", action, "t1", "t2", *HIS)
    assert refused.value.code == (
        f"task {action} takes no --quote: run task set <id> rank=<rank> --quote <his words> after it"
    )
