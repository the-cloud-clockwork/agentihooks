import json
import uuid
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger_agent_ops, ledger_duplicates, ledger_task_duplicates
from tests.swarm_ledger.test_ledger_authority import MASTER, SLUG, admin_put, cli_ledger, core, send
from tests.swarm_ledger.test_task_duplicate_check import REPEAT, Judge, authority_live, judged
from tests.swarm_ledger.test_task_duplicate_check import live as _task_live

live_tasks = _task_live
authority_live = authority_live
SEEN = "Retry the ledger watch when Redis drops the connection"
AGAIN = "Retry the ledger watch after Redis drops its connection"
FOLLOWUP_REFUSAL = (
    f'follow up f9 repeats follow up f1 "{SEEN}" (open, rank none, phase none): it is on the ledger and will be built. '
    'If it is a different change, add it with --not-duplicate "<why it differs>"'
)
TASK_REFUSAL = (
    'follow up f9 repeats task t1 "Publish the wheel to PyPI from main" (open, rank low, phase Release automation): '
    'it is on the ledger and will be built. If it is a different change, add it with --not-duplicate "<why it differs>"'
)
UNANSWERED = (
    "the duplicate check did not run for follow up f9 because the classifier did not answer, so it was added unchecked"
)


@pytest.fixture
def live(live_tasks):
    core.sync(SLUG, ops=[followup("f1", SEEN, by="swarm"), followup("f2", "Retry the ledger watch on a timeout")])
    core.sync(SLUG, ops=[{"op": "set", "id": "f2-done", "by": "swarm", "path": "followups/f2/done", "value": True}])
    return live_tasks


def followup(item_id, text, by=MASTER, **fields):
    return {"op": "add_item", "id": item_id, "by": by, "list": "followups", "text": text, **fields}


def operations(live, *ops):
    headers = {"X-Ledger-Token": live["admin"]}
    guards = {
        path: json.loads(send(live, "GET", f"/api/v1/ledgers/{SLUG}/{path}", **headers)[1])["revision"]
        for path in ("tasks", "followups")
    }
    body = {"operation_id": uuid.uuid4().hex, "ops": list(ops), "guards": guards}
    path = f"/api/v1/ledgers/{SLUG}/operations"
    status, data, _ = send(
        live, "POST", path, json.dumps(body).encode(), **headers, **{"Content-Type": "application/json"}
    )
    return status, json.loads(data)


def stored(item_id):
    return next((f for f in core.sync(SLUG)[0].get("followups", []) if f["id"] == item_id), None)


def test_a_repeated_follow_up_is_refused_naming_it(live, monkeypatch):
    judge = judged(monkeypatch, Judge(yes={"f1"}))
    status, reply = operations(live, followup("f9", AGAIN))
    assert (status, reply["rejected"], reply["applied"]) == (200, ["f9"], [])
    assert FOLLOWUP_REFUSAL in reply["_meta"]["warnings"]
    assert stored("f9") is None
    assert judge.asked == ["ledger-duplicate"] and judge.lock_free == [True]


def test_a_follow_up_repeating_an_open_task_is_refused_naming_the_task(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"t1"}))
    status, reply = operations(live, followup("f9", REPEAT))
    assert (status, reply["rejected"]) == (200, ["f9"])
    assert TASK_REFUSAL in reply["_meta"]["warnings"]
    assert stored("f9") is None


def test_the_page_transport_refuses_a_repeated_follow_up_too(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"f1"}))
    status, reply = admin_put(live, followup("f9", AGAIN))
    assert (status, reply["rejected"]) == (200, ["f9"])
    assert FOLLOWUP_REFUSAL in reply["_meta"]["warnings"]
    assert stored("f9") is None


def test_done_follow_ups_and_done_tasks_are_not_compared(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"f2", "t2"}))
    status, reply = operations(live, followup("f9", "Fold the chat panel and retry the ledger watch on a timeout"))
    assert (status, reply["rejected"]) == (200, [])
    assert stored("f9")["text"] == "Fold the chat panel and retry the ledger watch on a timeout"


def test_the_not_duplicate_reason_skips_the_check_and_is_stored(live, monkeypatch):
    judge = judged(monkeypatch, Judge(yes={"f1"}))
    reason = "this one covers the inbox watch and that one the ledger watch"
    status, reply = operations(live, followup("f9", AGAIN, not_duplicate=reason))
    assert (status, reply["rejected"]) == (200, [])
    assert stored("f9")["not_duplicate"] == reason
    assert judge.asked == []


def test_a_classifier_failure_lands_the_follow_up_with_a_warning(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"f1"}, error="unavailable"))
    status, reply = operations(live, followup("f9", AGAIN))
    assert (status, reply["rejected"]) == (200, [])
    assert UNANSWERED in reply["_meta"]["warnings"]
    assert stored("f9")["text"] == AGAIN


def test_a_task_and_a_follow_up_in_one_write_are_each_judged_by_their_kind(live, monkeypatch):
    judge = judged(monkeypatch, Judge(yes={"f1"}))
    add = {"op": "task_add", "id": "x", "by": MASTER, "task": "t9", "title": AGAIN, "lane": "eng"}
    status, reply = operations(live, add, followup("f9", AGAIN))
    assert (status, reply["rejected"]) == (200, ["f9"])
    assert judge.asked == ["ledger-duplicate", "ledger-duplicate"]
    assert stored("f9") is None


def cli(monkeypatch, *argv):
    monkeypatch.setattr(cli_ledger.uuid, "uuid4", lambda: SimpleNamespace(hex="f9" * 8))
    args = cli_ledger.build_parser().parse_args(["--slug", SLUG, "--as", MASTER, "followup", "add", AGAIN, *argv])
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "")
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    cli_ledger.cmd_followup(args)


def test_the_cli_exits_with_the_refusal(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"f1"}))
    with pytest.raises(SystemExit) as refused:
        cli(monkeypatch)
    assert FOLLOWUP_REFUSAL.replace("f9", "add_item-f9f9f9f9f9", 1) in str(refused.value)
    assert stored("add_item-f9f9f9f9f9") is None


def test_the_cli_override_lands_with_its_reason(live, monkeypatch):
    judge = judged(monkeypatch, Judge(yes={"f1"}))
    cli(monkeypatch, "--not-duplicate", "the inbox watch")
    assert stored("add_item-f9f9f9f9f9")["not_duplicate"] == "the inbox watch"
    assert judge.asked == []


def test_the_cli_flag_says_what_it_skips():
    parser = cli_ledger.build_parser()
    args = parser.parse_args(["--slug", SLUG, "--as", MASTER, "followup", "add", AGAIN])
    assert args.not_duplicate == ""
    text = " ".join(next(a for a in parser._subparsers._actions if a.choices).choices["followup"].format_help().split())
    assert (
        "--not-duplicate NOT_DUPLICATE why the follow up differs from the item it resembles; skips the duplicate check"
        in text
    )


@pytest.mark.parametrize(
    ("op", "screened"),
    [
        (followup("f9", AGAIN), True),
        (followup("f9", AGAIN, not_duplicate="the inbox watch"), False),
        ({"op": "add_item", "id": "q9", "by": MASTER, "list": "questions", "text": AGAIN}, False),
    ],
)
def test_which_item_adds_are_screened(op, screened):
    assert ledger_task_duplicates.checked(op) is screened


def test_a_follow_up_refusal_names_the_follow_up_it_repeats():
    match = {"id": "f1", "kind": "followup", "title": SEEN, "state": "open", "rank": None, "phase_title": None}
    assert ledger_task_duplicates.refusal(followup("f9", AGAIN), match) == FOLLOWUP_REFUSAL


def test_the_screen_asks_the_finder_once_per_kind(monkeypatch):
    asked = []
    monkeypatch.setattr(
        ledger_task_duplicates, "find", lambda doc, adds, kind: asked.append(kind) or [None] * len(adds)
    )
    add = {"op": "task_add", "id": "x", "by": MASTER, "task": "t9", "title": AGAIN, "lane": "eng"}
    ledger_task_duplicates.screen({}, [followup("f9", AGAIN), add, followup("f8", SEEN)])
    assert asked == ["task", "followup"]


def test_the_finder_pool_for_a_follow_up_holds_open_follow_ups_and_open_tasks():
    doc = {
        "followups": [{"id": "f1"}, {"id": "f2", "done": True}, {"id": "f3", "out_of_scope": True}],
        "tasks": [
            {"id": "t1", "state": "open"},
            {"id": "t2", "state": "done"},
            {"id": "t3", "state": "open", "out_of_scope": True},
        ],
    }
    assert ledger_duplicates._pool(doc, "followup") == [
        ("followup", {"id": "f1"}),
        ("task", {"id": "t1", "state": "open"}),
    ]


@pytest.mark.parametrize(
    "op",
    [
        followup("f9", AGAIN, not_duplicate=" "),
        followup("f9", AGAIN, not_duplicate=5),
        {"op": "add_item", "id": "q9", "by": MASTER, "list": "questions", "text": AGAIN, "not_duplicate": "a reason"},
    ],
)
def test_a_not_duplicate_reason_must_be_plain_words_on_a_follow_up(op):
    with pytest.raises(ValueError) as refused:
        ledger_agent_ops.check(op)
    assert str(refused.value) == (
        "not_duplicate must say in plain words why the follow up differs from the item it resembles"
    )


def test_a_follow_up_add_with_a_reason_passes_the_check():
    ledger_agent_ops.check(followup("f9", AGAIN, not_duplicate="the inbox watch"))


class Inner:
    def __init__(self, accepted=True):
        self.applied, self.accepted = [], accepted

    def apply(self, doc, op, ctx, apply_op):
        self.applied.append((op["id"], apply_op))
        return self.accepted


def test_a_follow_up_repeating_one_that_landed_during_its_check_is_refused():
    screen = ledger_task_duplicates.Screen(seen=frozenset({"followups/f9"}))
    gate = ledger_task_duplicates.Gate(screen, Inner(accepted=False))
    landed = {"id": "f4", "text": f"  {AGAIN.upper()} "}
    assert gate.apply({"followups": [landed]}, followup("f9", AGAIN), None, "apply") is False
    (applied_id, refuse), ctx = gate.inner.applied[0], SimpleNamespace(refused=[])
    assert applied_id == "f9" and refuse({}, {}, ctx) is False
    assert ctx.refused == [
        f'follow up f9 repeats follow up f4 "{landed["text"]}", added while its duplicate check ran. '
        'If it is a different change, add it with --not-duplicate "<why it differs>"'
    ]


def test_a_follow_up_names_the_open_follow_ups_that_landed_while_its_check_ran():
    screen = ledger_task_duplicates.Screen(seen=frozenset({"followups/f1", "followups/f9"}))
    gate = ledger_task_duplicates.Gate(screen, Inner())
    doc = {
        "tasks": [{"id": "t4", "title": "Other"}],
        "followups": [
            {"id": "f1", "text": "Seen"},
            {"id": "f9", "text": AGAIN},
            {"id": "f4", "text": "Other"},
            {"id": "f5", "text": "Closed", "done": True},
        ],
    }
    assert gate.apply(doc, followup("f9", AGAIN), None, "apply") is True
    assert screen.warnings == {
        "f9": "the duplicate check did not compare follow up f9 with f4, added while it ran, so check them for repeats"
    }
    assert gate.inner.applied == [("f9", "apply")]


def test_the_screen_remembers_the_follow_ups_it_compared_and_the_adds_it_screened(monkeypatch):
    monkeypatch.setattr(ledger_task_duplicates, "find", lambda doc, adds, kind: [None] * len(adds))
    doc = {"tasks": [{"id": "t1"}], "followups": [{"id": "f1"}]}
    ops = [
        followup("f9", AGAIN),
        followup("f8", AGAIN, not_duplicate="the inbox watch"),
        {"op": "add_item", "id": "q9", "by": MASTER, "list": "questions", "text": AGAIN},
    ]
    assert ledger_task_duplicates.screen(doc, ops).seen == frozenset(
        {"t1", "followups/f1", "followups/f9", "followups/f8"}
    )
