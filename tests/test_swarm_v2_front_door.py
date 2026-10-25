import json

import pytest

import scripts.swarm_ledger.repository  # noqa: F401  puts the ledger folder on sys.path before ledger_core loads
from scripts.gates import log
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_server
from scripts.swarm_v2 import front_door
from tests import sv2_mst04_cases as cases
from tests.sv2_mst04_cases import ENGINEER, LEAD, LEAD_NAME, SECOND, SECOND_NAME, SLUG

pytestmark = pytest.mark.unit

SEATLESS = "master@abcdef-0009"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path / "ledgers")
    core.LEDGER_DIR.mkdir()
    return cases.World()


OPERATOR_LINE = "o-1"


def operator_says(world, text="how is the deploy going"):
    core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": OPERATOR_LINE, "text": text}])


def write(world, op, connect=None, home=None):
    door = front_door.Door(SLUG, connect=connect or (lambda: world.redis), home=home)
    state, rejected = core.sync(SLUG, ops=[op], gate=door)
    return op["id"] not in rejected, state["_meta"]["warnings"]


def chat(by, kind="add", n=0, text="hello from the masters", reply_to=OPERATOR_LINE):
    op = {"op": kind, "thread": "chat", "id": f"m-{n}", "by": by, "text": text, "to": "operator"}
    return {**op, "reply_to": reply_to} if kind == "add" and reply_to else op


def chat_texts(world):
    return [(entry["by"], entry["text"]) for entry in world.doc()["chat"] if entry["by"] != "operator"]


def test_the_front_door_is_the_bare_master_seat():
    assert front_door.seat("sw") == "master@sw"
    assert [front_door.holds("sw", held) for held in ("master@sw", "", "master-2@sw", "master@other")] == [
        True,
        True,
        False,
        False,
    ]


def test_the_refusal_names_the_inbox_route_to_the_front_door():
    assert front_door.refusal("sw", "master-2@sw") == (
        "page chat belongs to the front door master master@sw, so master-2@sw cannot write it; send it to the front "
        'door master by inbox with agentihooks msg send master@sw "<text>"'
    )


def test_the_say_refusal_names_the_inbox_route_to_the_front_door():
    assert front_door.say_refusal("sw", "engineer@abc-1") == (
        "no agent writes to the operator first, so engineer@abc-1 cannot say to him; the front door answers him only "
        'when he writes; send it to the front door master by inbox with agentihooks msg send master@sw "<text>"'
    )


def test_the_ask_refusal_names_the_inbox_route_to_the_front_door():
    assert front_door.ask_refusal("sw", "engineer@abc-1") == (
        "no agent asks the operator first, so engineer@abc-1 cannot add a question, priority or needs operator "
        "follow up; the front door answers him only when he writes; send it to the front door master by inbox with "
        'agentihooks msg send master@sw "<text>"'
    )


@pytest.mark.parametrize(
    ("op", "covered"),
    [
        ({"op": "add", "thread": "chat", "by": "m"}, True),
        ({"op": "edit", "thread": "chat", "by": "m"}, True),
        ({"op": "add", "thread": "chat"}, False),
        ({"op": "delete", "thread": "chat", "by": "m"}, True),
        ({"op": "clear", "thread": "chat", "by": "m"}, False),
        ({"op": "add", "thread": "followups/f1/comments", "by": "m"}, False),
        ({"op": "answer", "item": "questions/q1", "by": "m"}, False),
    ],
)
def test_only_agent_chat_adds_and_edits_are_covered(op, covered):
    assert front_door.chat_write(op) is covered


def gate_rows(home):
    return [json.loads(line) for line in log.gate_log_path(SLUG, home).read_text().splitlines()]


def test_a_backend_master_chat_write_is_refused_naming_the_front_door(world, tmp_path):
    accepted, warnings = write(world, chat(SECOND_NAME), home=tmp_path)

    assert accepted is False
    assert warnings == [front_door.refusal(SLUG, SECOND)]
    assert LEAD in warnings[0]
    assert chat_texts(world) == []
    assert [(r["gate"], r["kind"], r["agent"], r["tool"], r["reason"]) for r in gate_rows(tmp_path)] == [
        (front_door.NAME, "deny", SECOND_NAME, "ledger", warnings[0])
    ]


def test_a_former_backend_occupant_stays_refused(world):
    world.store.seats.occupy(SECOND, cases.SUCCESSOR, 2)

    assert write(world, chat(SECOND_NAME)) == (False, [front_door.refusal(SLUG, SECOND)])
    assert write(world, chat(cases.SUCCESSOR, n=1)) == (False, [front_door.refusal(SLUG, SECOND)])


def test_a_backend_master_chat_delete_is_refused(world):
    operator_says(world)
    assert write(world, chat(LEAD_NAME))[0] is True

    accepted, _ = write(world, {"op": "delete", "thread": "chat", "id": "m-0", "by": SECOND_NAME})

    assert accepted is False
    assert chat_texts(world) == [(LEAD_NAME, "hello from the masters")]


def test_a_backend_master_chat_edit_is_refused(world):
    operator_says(world)
    assert write(world, chat(LEAD_NAME))[0] is True

    accepted, warnings = write(world, chat(SECOND_NAME, kind="edit", text="changed by master two"))

    assert (accepted, warnings) == (False, [front_door.refusal(SLUG, SECOND)])
    assert chat_texts(world) == [(LEAD_NAME, "hello from the masters")]


@pytest.mark.parametrize("by", [LEAD_NAME, SEATLESS])
def test_a_front_door_reply_to_an_operator_line_is_accepted(world, by):
    operator_says(world)

    assert write(world, chat(by)) == (True, [])
    assert [(e["by"], e.get("reply_to")) for e in world.doc()["chat"]] == [("operator", None), (by, OPERATOR_LINE)]


@pytest.mark.parametrize("by", [LEAD_NAME, SEATLESS, SECOND_NAME, ENGINEER])
def test_an_unprompted_chat_write_is_refused_for_every_agent(world, by):
    accepted, warnings = write(world, chat(by, reply_to=""))

    assert (accepted, len(warnings)) == (False, 1)
    assert "agentihooks msg send master@" in warnings[0]
    assert chat_texts(world) == []


def test_the_front_door_cannot_reply_to_its_own_or_a_missing_line(world):
    operator_says(world)
    assert write(world, chat(LEAD_NAME))[0] is True

    assert write(world, chat(LEAD_NAME, n=1, reply_to="m-0"))[0] is False
    assert write(world, chat(LEAD_NAME, n=2, reply_to="o-9"))[0] is False
    assert chat_texts(world) == [(LEAD_NAME, "hello from the masters")]


@pytest.mark.parametrize("by", [SECOND_NAME, ENGINEER])
def test_a_non_front_door_reply_to_an_operator_line_is_refused(world, by):
    operator_says(world)
    held = SECOND if by == SECOND_NAME else by

    assert write(world, chat(by)) == (False, [front_door.refusal(SLUG, held)])
    assert chat_texts(world) == []


def test_the_swarm_receipt_still_replies_to_an_operator_line(world):
    operator_says(world)
    receipt = {"op": "add", "thread": "chat", "id": "received-1", "by": "swarm", "to": "operator"}

    assert write(world, {**receipt, "text": "Received by the master inbox.", "reply_to": OPERATOR_LINE}) == (True, [])


ASKS = [
    {"op": "add_item", "id": "a-1", "list": "questions", "text": "Which port should the probe use"},
    {"op": "add_item", "id": "a-2", "list": "followups", "text": "Decide the port", "needs_operator": True},
    {"op": "set", "id": "a-3", "path": "followups/f1/needs_operator", "value": True},
    {"op": "priority", "id": "a-4", "item": "followups/f1", "text": "Decide the port"},
]


@pytest.mark.parametrize("ask", ASKS, ids=lambda ask: ask["id"])
@pytest.mark.parametrize("by", [LEAD_NAME, SECOND_NAME, ENGINEER])
def test_every_agent_ask_to_the_operator_is_refused(world, ask, by):
    accepted, warnings = write(world, {**ask, "by": by})

    assert (accepted, warnings) == (False, [front_door.ask_refusal(SLUG, by)])


@pytest.mark.parametrize(
    "op",
    [
        {"op": "add_item", "list": "followups", "text": "x"},
        {"op": "add_item", "list": "followups", "text": "x", "needs_operator": False},
        {"op": "set", "path": "followups/f1/needs_operator", "value": False},
        {"op": "set", "path": "followups/f1/done", "value": True},
        {"op": "set", "value": True},
        {"op": "priority_clear", "target": "all"},
    ],
)
def test_other_item_writes_are_no_ask(op):
    assert front_door.asks_operator(op) is False


def test_the_swarm_still_raises_a_needs_operator_follow_up(world):
    op = {
        "op": "add_item",
        "id": "s-1",
        "by": "swarm",
        "list": "followups",
        "text": "Unread item",
        "needs_operator": True,
    }

    assert write(world, op) == (True, [])


def test_a_backend_master_still_comments_on_items(world):
    op = {"op": "add", "thread": f"{cases.FOLLOWUP}/comments", "id": "c-1", "by": SECOND_NAME, "text": "on it now"}

    assert write(world, op) == (True, [])


def test_other_writes_never_reach_redis(world):
    def unreachable():
        raise AssertionError("the front door connected to Redis for a write it does not cover")

    assert write(world, chat(ENGINEER), connect=unreachable) == (False, [front_door.refusal(SLUG, ENGINEER)])


def test_a_crashed_seat_lookup_lets_the_write_through_and_logs_it(world, tmp_path):
    operator_says(world)

    def broken():
        raise ConnectionError("redis down")

    assert write(world, chat(SECOND_NAME), connect=broken, home=tmp_path) == (True, [])
    assert [(r["gate"], r["kind"], r["agent"], r["tool"], r["reason"]) for r in gate_rows(tmp_path)] == [
        (front_door.NAME, "fail-open", SECOND_NAME, "ledger", "ConnectionError: redis down")
    ]


def test_the_door_passes_admitted_writes_to_its_inner_gate(world):
    seen = []

    class Inner:
        def apply(self, doc, op, ctx, apply):
            seen.append(op["id"])
            return apply(doc, op, ctx)

    operator_says(world)
    door = front_door.Door(SLUG, Inner(), connect=lambda: world.redis)
    _, rejected = core.sync(SLUG, ops=[chat(LEAD_NAME, n=1), chat(SECOND_NAME, n=2)], gate=door)

    assert (seen, set(rejected)) == (["m-1"], {"m-2"})


def test_an_authorless_write_passes_without_an_author_lookup():
    class Ctx:
        refused = []

        def author(self, name):
            raise AssertionError("the front door looked up an author for a write without one")

    door = front_door.Door(SLUG, connect=lambda: None)
    op = {"op": "add", "thread": "chat", "id": "m-1", "text": "hello"}

    assert door.apply({}, op, Ctx(), lambda doc, op, ctx: "applied") == "applied"


def test_the_server_write_gate_refuses_a_backend_master_chat_write_naming_the_front_door(world, monkeypatch):
    monkeypatch.setattr(front_door, "_connect", lambda: world.redis)
    operator_says(world)
    ops = [chat(SECOND_NAME, n=1), chat(LEAD_NAME, n=2)]

    state, rejected = core.sync(SLUG, ops=ops, gate=ledger_server.write_gate(SLUG))

    assert set(rejected) == {"m-1"}
    assert state["_meta"]["warnings"] == [front_door.refusal(SLUG, SECOND)]
    assert chat_texts(world) == [(LEAD_NAME, "hello from the masters")]
