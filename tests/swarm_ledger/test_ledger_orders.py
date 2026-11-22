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


def order(item, by=MASTER, **fields):
    return write("order_set", by=by, item=item, **fields)


def row(state, item):
    name, item_id = item.split("/")
    return next(found for found in state[name] if found["id"] == item_id)


def event_of(state):
    event = state["_meta"]["events"][-1]
    return {key: value for key, value in event.items() if key not in ("rev", "at")}


def refusals(state):
    return [warning for warning in state["_meta"]["warnings"] if warning.startswith("order on ")]


def cli(monkeypatch, *argv, by=MASTER):
    def call(slug, ops=None):
        state, rejected = core.sync(slug, ops=ops)
        return {**state, "rejected": rejected, "applied": [op["id"] for op in ops or [] if op["id"] not in rejected]}

    monkeypatch.setattr(ledger, "call", call)
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", by, *argv])
    getattr(ledger, f"cmd_{args.command}")(args)


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
    assert event_of(state) == {"by": "operator", "kind": "ordered", "target": item, "id": event_of(state)["id"]} | marks
    assert state["_meta"]["stamps"][f"{item}/ordered"]["by"] == "operator"


@pytest.mark.feature("ledger-order-mark")
def test_a_ledger_entry_the_operator_wrote_marks_the_item_it_asks_for():
    asked = write("add", thread="chat", text="Build the reviewer seats")[0]["chat"][-1]["id"]
    state, rejected = order("phases/p1", request=asked)
    assert rejected == []
    mark = row(state, "phases/p1")["ordered"]
    assert mark == {"at": mark["at"], "by": MASTER, "request": asked}
    marks = {"relayed_by": MASTER, "relayed_from": "master pane", "request": asked}
    ordered = {"by": "operator", "kind": "ordered", "target": "phases/p1", "id": event_of(state)["id"]}
    assert event_of(state) == ordered | marks


@pytest.mark.feature("ledger-order-mark")
def test_an_entry_an_agent_wrote_orders_nothing():
    write("add", by=ENGINEER, thread="phases/p1/comments", text="Started the build")
    comment = core.sync(SLUG)[0]["phases"][0]["comments"][-1]["id"]
    for request in (comment, "no-such-entry"):
        state, rejected = order("phases/p1", request=request)
        assert len(rejected) == 1
        assert "ordered" not in row(state, "phases/p1")
        assert refusals(state) == [f"order on phases/p1 refused: the entry {request} is not one the operator wrote"]


@pytest.mark.feature("ledger-order-mark")
@pytest.mark.parametrize("quote", ["ship the warm writer first", "reviewer seats"])
def test_words_the_operator_never_said_order_nothing(quote):
    state, rejected = order("tasks/t1", quote=quote)
    assert len(rejected) == 1
    assert "ordered" not in row(state, "tasks/t1")
    assert refusals(state) == [f"order on tasks/t1 refused: {UNVERIFIED}"]


@pytest.mark.feature("ledger-order-mark")
@pytest.mark.parametrize("proof", [{"quote": QUOTE}, {"request": "no-such-entry"}])
def test_only_the_orchestrator_relays_an_order(proof):
    operator_words.record(ENGINEER, SAID)
    state, rejected = order("phases/p1", by=ENGINEER, **proof)
    assert len(rejected) == 1
    assert "ordered" not in row(state, "phases/p1")
    assert refusals(state) == [f"order on phases/p1 refused: {ENGINEER} is not an orchestrator of this ledger"]


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
    assert event_of(state) == {"by": "operator", "kind": "ordered", "target": "tasks/t1", "id": event_of(state)["id"]}


@pytest.mark.feature("ledger-order-mark")
def test_the_operators_own_order_keeps_the_words_he_gives_unchecked():
    state, rejected = order("phases/p1", by=None, quote="ship the warm writer first")
    assert rejected == []
    mark = row(state, "phases/p1")["ordered"]
    assert mark == {"at": mark["at"], "by": "operator", "quote": "ship the warm writer first"}


@pytest.mark.feature("ledger-order-mark")
def test_a_second_order_keeps_the_first_mark():
    first = row(order("phases/p1", quote=QUOTE)[0], "phases/p1")["ordered"]
    events = len(core.sync(SLUG)[0]["_meta"]["events"])
    state, rejected = order("phases/p1", by=None)
    assert rejected == []
    assert row(state, "phases/p1")["ordered"] == first
    assert len(state["_meta"]["events"]) == events


@pytest.mark.feature("ledger-order-mark")
def test_only_the_operator_or_his_words_clear_an_order():
    order("phases/p1", quote=QUOTE)
    state, rejected = write("order_clear", by=ENGINEER, item="phases/p1", quote=QUOTE)
    assert len(rejected) == 1
    assert refusals(state) == [f"order on phases/p1 refused: {ENGINEER} is not an orchestrator of this ledger"]
    state, rejected = write("order_clear", by=MASTER, item="phases/p1", quote="drop the reviewer seats")
    assert len(rejected) == 1
    assert refusals(state) == [f"order on phases/p1 refused: {UNVERIFIED}"]
    assert "ordered" in row(state, "phases/p1")
    state, rejected = write("order_clear", by=MASTER, item="phases/p1", quote="reviewer seats today before")
    assert rejected == []
    assert "ordered" not in row(state, "phases/p1")
    marks = {"relayed_by": MASTER, "relayed_from": "master pane", "quote": "reviewer seats today before"}
    cleared = {"by": "operator", "kind": "order cleared", "target": "phases/p1", "id": event_of(state)["id"]}
    assert event_of(state) == cleared | marks
    assert state["_meta"]["stamps"]["phases/p1/ordered"]["rev"] == state["_meta"]["rev"]
    order("phases/p1", by=None)
    state, rejected = write("order_clear", item="phases/p1")
    assert rejected == []
    assert "ordered" not in row(state, "phases/p1")
    assert event_of(state) == {"by": "operator", "kind": "order cleared", "target": "phases/p1"} | {
        "id": event_of(state)["id"]
    }


@pytest.mark.feature("ledger-order-mark")
def test_clearing_an_item_that_carries_no_order_changes_nothing():
    events = len(core.sync(SLUG)[0]["_meta"]["events"])
    state, rejected = write("order_clear", item="tasks/t1")
    assert rejected == []
    assert len(state["_meta"]["events"]) == events
    state, rejected = write("order_clear", by=ENGINEER, item="tasks/t1", quote=QUOTE)
    assert len(rejected) == 1


@pytest.mark.feature("ledger-order-mark")
def test_phase_and_task_updates_keep_the_order():
    order("phases/p1", quote=QUOTE)
    order("tasks/t1", quote=QUOTE)
    write("phase_update", by=MASTER, item="phases/p1", fields={"title": "Build the seats"})
    state, rejected = write("task_update", by=MASTER, item="tasks/t1", fields={"territory": ["ledger"]})
    assert rejected == []
    assert row(state, "phases/p1")["title"] == "Build the seats"
    assert row(state, "phases/p1")["ordered"]["quote"] == HEARD
    assert row(state, "tasks/t1")["territory"] == ["ledger"]
    assert row(state, "tasks/t1")["ordered"]["quote"] == HEARD


@pytest.mark.feature("ledger-order-mark")
@pytest.mark.parametrize(
    ("op", "message"),
    [
        ({"op": "order_set", "by": MASTER, "item": "followups/f1", "quote": QUOTE}, "an order names phases/<id>"),
        ({"op": "order_set", "by": MASTER, "item": "phases/p1/comments", "quote": QUOTE}, "an order names phases/<id>"),
        ({"op": "order_set", "by": MASTER, "item": "tasks/", "quote": QUOTE}, "an order names phases/<id>"),
        ({"op": "order_set", "by": MASTER, "quote": QUOTE}, "an order names phases/<id>"),
        ({"op": "order_set", "by": MASTER, "item": "phases/p1"}, "an agent's order needs quote"),
        ({"op": "order_set", "by": MASTER, "item": "phases/p1", "quote": QUOTE, "request": "c1"}, "an agent's order"),
        ({"op": "order_set", "by": MASTER, "item": "phases/p1", "quote": "  "}, "quote must be text"),
        ({"op": "order_set", "by": MASTER, "item": "phases/p1", "quote": 7}, "quote must be text"),
        ({"op": "order_set", "by": MASTER, "item": "phases/p1", "request": ""}, "request must be text"),
        ({"op": "order_set", "by": "operator", "item": "phases/p1", "quote": QUOTE}, "order ops take by"),
        ({"op": "order_set", "by": 7, "item": "phases/p1", "quote": QUOTE}, "order ops take by"),
        ({"op": "order_set", "by": "no name", "item": "phases/p1", "quote": QUOTE}, "order ops take by"),
        ({"op": "order_set", "by": MASTER, "item": "phases/p1", "quote": QUOTE, "rank": "urgent"}, "takes only"),
        ({"op": "order_set", "by": MASTER, "item": "phases/p1", "quote": "x" * 20001}, "up to 20000 characters"),
        ({"op": "order_clear", "by": MASTER, "item": "phases/p1"}, "an agent clears an order only with quote"),
        (
            {"op": "order_clear", "by": MASTER, "item": "phases/p1", "request": "c1"},
            "order_clear takes only item quote",
        ),
    ],
)
def test_a_malformed_order_is_refused_at_check(op, message):
    with pytest.raises(ValueError, match=message):
        core.check_op({"id": "o1", **op})


@pytest.mark.feature("ledger-order-mark")
def test_a_well_formed_order_passes_check():
    for op in (
        {"op": "order_set", "item": "phases/p1"},
        {"op": "order_set", "by": MASTER, "item": "tasks/t1", "quote": "x" * 20000},
        {"op": "order_set", "by": MASTER, "item": "tasks/t1", "request": "c1"},
        {"op": "order_clear", "item": "tasks/t1"},
        {"op": "order_clear", "by": MASTER, "item": "tasks/t1", "quote": QUOTE},
    ):
        core.check_op({"id": "o1", **op})


@pytest.mark.feature("ledger-order-mark")
def test_order_ops_are_in_the_api_contract_and_target_their_item():
    assert schemas.FIELDS["order_set"] == "by item quote request"
    assert schemas.FIELDS["order_clear"] == "by item quote"
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


@pytest.mark.feature("ledger-order-at-filing")
def test_phase_add_with_his_words_files_and_orders_the_phase_in_one_call(monkeypatch, capsys):
    calls, real = spied(monkeypatch)
    cli(monkeypatch, "phase", "add", "p7", "Reviewer", "seats", "--description", "Seats", "--quote", QUOTE)
    assert [[op["op"] for op in ops] for ops in calls if ops] == [["phase_add", "order_set"]]
    state = real(SLUG)[0]
    assert row(state, "phases/p7")["title"] == "Reviewer seats"
    mark = row(state, "phases/p7")["ordered"]
    assert mark == {"at": mark["at"], "by": MASTER, "quote": HEARD}
    assert json.loads(capsys.readouterr().out)["title"] == "Reviewer seats"


@pytest.mark.feature("ledger-order-at-filing")
def test_task_add_with_his_words_files_and_orders_the_task_in_one_call(monkeypatch, capsys):
    calls, real = spied(monkeypatch)
    cli(monkeypatch, "task", "add", "t7", "Fix", "the", "writer", "--phase", "p1", "--quote", QUOTE)
    assert [[op["op"] for op in ops] for ops in calls if ops] == [["task_add", "order_set"]]
    state = real(SLUG)[0]
    assert row(state, "tasks/t7")["title"] == "Fix the writer"
    assert row(state, "tasks/t7")["ordered"]["quote"] == HEARD
    assert json.loads(capsys.readouterr().out)["task"] == "t7"


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
def test_a_filing_whose_order_the_server_refuses_says_the_entry_was_filed(monkeypatch):
    operator_words.record("planner", SAID)
    with pytest.raises(SystemExit) as refused:
        cli(monkeypatch, "task", "add", "t7", "Fix the writer", "--phase", "p1", "--quote", QUOTE, by="planner")
    reason = "order on tasks/t7 refused: planner is not an orchestrator of this ledger"
    assert refused.value.code == f"filed without its order: {reason}"
    assert "ordered" not in row(core.sync(SLUG)[0], "tasks/t7")


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
        cli(monkeypatch, "task", action, "t1", "t2", "--quote", QUOTE)
    assert str(refused.value.code).startswith(f"task {action} takes no --quote")
