import json
import subprocess
import sys
import unittest
from pathlib import Path

import pytest

from hooks.context import conditions
from tests.swarm_ledger.test_flat_buttons import css_rules, declarations
from tests.swarm_ledger.test_one_line_ids import function_source, style

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_verdict  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "verdict-2026-01-01"
SID = "sid-verdict"
CONDITION_ASK = "Wave one gate shims need your words set a condition"


def make_ledger():
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [],
        "phases": [{"title": "one", "description": "d"}],
        "questions": [{"text": "which broker?"}],
        "followups": [{"text": "check disk"}],
        "tasks": [{"title": "Wave one gate shims", "description": "d", "phase": "p1", "lane": "eng"}],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)
    core.sync(SLUG, ops=[{"op": "join", "id": "j1", "by": "eng-1"}])


def priority(item, text, pid="pr-1"):
    return {"op": "priority", "id": pid, "by": "eng-1", "item": item, "text": text}


def verdict(item, said, vid="v-1"):
    return {"op": "verdict", "id": vid, "item": item, "verdict": said}


def item_of(state, path):
    name, item_id = path.split("/")
    return next(i for i in state[name] if i["id"] == item_id)


def priorities_on(state, path):
    return [p["id"] for p in state["priorities"] if p["item"] == path]


def operator_comments(state, path):
    return [(c["id"], c["by"], c["text"]) for c in item_of(state, path)["comments"]]


class Verdict(unittest.TestCase):
    def setUp(self):
        make_ledger()

    def test_approve_on_a_priority_writes_one_comment_with_the_ask_and_removes_the_priority(self):
        core.sync(SLUG, ops=[priority("followups/f1", "Decide which disk to check")])
        state, rejected = core.sync(SLUG, ops=[verdict("followups/f1", "approved")])
        self.assertEqual(rejected, [])
        self.assertEqual(
            operator_comments(state, "followups/f1"), [("v-1", "operator", "approved, Decide which disk to check")]
        )
        self.assertEqual(priorities_on(state, "followups/f1"), [])

    def test_a_condition_ask_is_approved_as_set_a_condition(self):
        core.sync(SLUG, ops=[priority("tasks/t1", CONDITION_ASK)])
        state, _ = core.sync(SLUG, ops=[verdict("tasks/t1", "approved")])
        self.assertEqual(operator_comments(state, "tasks/t1"), [("v-1", "operator", "approved, set a condition")])

    def test_approve_and_deny_on_items_without_a_priority_write_one_comment_each(self):
        state, _ = core.sync(
            SLUG, ops=[verdict("phases/p1", "approved", "v-1"), verdict("questions/q1", "denied", "v-2")]
        )
        self.assertEqual(operator_comments(state, "phases/p1"), [("v-1", "operator", "approved")])
        self.assertEqual(operator_comments(state, "questions/q1"), [("v-2", "operator", "denied")])

    def test_deny_posts_denied_and_clears_the_priority_on_the_item(self):
        core.sync(SLUG, ops=[priority("tasks/t1", CONDITION_ASK)])
        state, _ = core.sync(SLUG, ops=[verdict("tasks/t1", "denied")])
        self.assertEqual(operator_comments(state, "tasks/t1"), [("v-1", "operator", "denied")])
        self.assertEqual(priorities_on(state, "tasks/t1"), [])

    def test_the_comment_is_an_operator_write_the_inbox_relays(self):
        state, _ = core.sync(SLUG, ops=[verdict("tasks/t1", "approved")])
        event = next(e for e in state["_meta"]["events"] if e.get("id") == "v-1")
        self.assertEqual(
            {k: event[k] for k in ("by", "kind", "target", "text")},
            {"by": "operator", "kind": "comment added", "target": "tasks/t1", "text": "approved"},
        )

    def test_a_repeated_verdict_writes_once(self):
        core.sync(SLUG, ops=[verdict("tasks/t1", "approved")])
        state, rejected = core.sync(SLUG, ops=[verdict("tasks/t1", "approved")])
        self.assertEqual(rejected, [])
        self.assertEqual(len(item_of(state, "tasks/t1")["comments"]), 1)

    def test_a_missing_item_is_rejected(self):
        _, rejected = core.sync(SLUG, ops=[verdict("tasks/nope", "approved")])
        self.assertEqual(len(rejected), 1)


@pytest.mark.parametrize(
    ("op", "message"),
    [
        ({"op": "verdict", "id": "v", "item": "tasks", "verdict": "approved"}, "verdict needs item <list>/<id>"),
        ({"op": "verdict", "id": "v", "item": "tasks/t1", "verdict": "maybe"}, "verdict must be approved or denied"),
        (
            {"op": "verdict", "id": "v", "item": "tasks/t1", "verdict": "approved", "by": "eng-1"},
            "verdict is the operator's",
        ),
    ],
)
def test_check_refuses(op, message):
    with pytest.raises(ValueError) as caught:
        ledger_verdict.check(op)
    assert str(caught.value) == message


def test_check_passes_a_well_formed_verdict():
    for said in ledger_verdict.VERDICTS:
        assert ledger_verdict.check({"op": "verdict", "id": "v", "item": "tasks/t1", "verdict": said}) is None


class StubCtx:
    def __init__(self):
        self.at, self.meta, self.events, self.stamps = 5, {}, [], []

    def record(self, by, kind, target, **extra):
        self.events.append((by, kind, target, extra))

    def stamp(self, path, by):
        self.stamps.append((path, by))


def stub_doc(**task):
    return {"phases": [], "tasks": [{"id": "t1", "comments": [], **task}]}


def test_apply_stamps_the_comment_thread_and_clears_the_items_priority():
    doc, ctx = stub_doc(), StubCtx()
    doc["priorities"] = [{"id": "p1", "item": "tasks/t1", "text": "Pick a disk", "by": "eng-1"}]
    assert ledger_verdict.apply(doc, verdict("tasks/t1", "approved"), ctx) is True
    assert doc["tasks"][0]["comments"] == [{"id": "v-1", "by": "operator", "at": 5, "text": "approved, Pick a disk"}]
    assert ctx.stamps == [("tasks/t1/comments", "operator")]
    assert doc["priorities"] == []
    assert [e[1] for e in ctx.events] == ["comment added", "priority cleared"]


def test_apply_works_on_a_ledger_without_priorities():
    doc, ctx = stub_doc(), StubCtx()
    assert ledger_verdict.apply(doc, verdict("tasks/t1", "denied"), ctx) is True
    assert doc["tasks"][0]["comments"][0]["text"] == "denied"


def test_apply_refuses_a_deleted_item():
    doc, ctx = stub_doc(deleted=True), StubCtx()
    assert ledger_verdict.apply(doc, verdict("tasks/t1", "approved"), ctx) is False
    assert doc["tasks"][0]["comments"] == []


def test_an_approved_condition_ask_opens_the_condition_gate_for_that_task(monkeypatch):
    make_ledger()
    core.sync(SLUG, ops=[priority("tasks/t1", CONDITION_ASK)])
    monkeypatch.setenv("LEDGER_DIR", str(core.LEDGER_DIR))
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t1")
    write = ("mcp__agentihooks__condition_set", {"step": "pre"}, SID)
    assert conditions.write_guard(*write) == conditions.GATE_MESSAGE
    core.sync(SLUG, ops=[verdict("tasks/t1", "approved", "v-9")])
    assert conditions.write_guard(*write) is None
    assert conditions.gate_source(SID) == {"source": "ledger", "ref": "v-9"}


def run_page(body):
    stubs = (
        "const h = (tag, attrs, ...kids) => ({ tag, attrs: attrs || {}, kids: kids.filter(Boolean) });"
        "const scopeDot = () => ({ tag: 'scope', attrs: {}, kids: [] }); const queued = [];"
        "const queue = (op) => queued.push(op); const newId = (noun) => `${noun[0]}-1`;"
        "const doc = { freezes: [] }; const frozen = () => false; const freezeButton = () => null;"
    )
    names = ("verdictButton", "itemActions", "itemOf", "applyOp")
    script = stubs + "\n".join(function_source(n) for n in names) + "\n" + body
    out = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def test_every_item_row_carries_approve_then_deny_then_out_of_scope():
    out = run_page(
        "const row = itemActions('tasks/t1', {}); row.kids.forEach((b) => b.attrs.on && b.attrs.on.click());"
        "process.stdout.write(JSON.stringify({ row: row.kids.map((k) => [k.tag, k.attrs.class || '', k.attrs.text || '']),"
        " queued }));"
    )
    assert out["row"] == [["button", "link approve", "Approve"], ["button", "link deny", "Deny"], ["scope", "", ""]]
    assert out["queued"] == [
        {"op": "verdict", "id": "c-1", "item": "tasks/t1", "verdict": "approved"},
        {"op": "verdict", "id": "c-1", "item": "tasks/t1", "verdict": "denied"},
    ]


def test_a_verdict_shows_at_once_and_drops_the_items_priority():
    out = run_page(
        "const d = { tasks: [{ id: 't1', comments: [] }],"
        " priorities: [{ id: 'p', item: 'tasks/t1' }, { id: 'q', item: 'tasks/t2' }] };"
        "applyOp(d, { op: 'verdict', id: 'c-1', item: 'tasks/t1', verdict: 'approved' });"
        "process.stdout.write(JSON.stringify({ comments: d.tasks[0].comments.map((c) => [c.id, c.by, c.text]),"
        " priorities: d.priorities.map((p) => p.id) }));"
    )
    assert out == {"comments": [["c-1", "operator", "approved"]], "priorities": ["q"]}


def test_each_priority_row_offers_approve_before_clear():
    render = function_source("renderPriorities")
    assert render.index('verdictButton(p.item, "approved", "approve", "Approve")') < render.index('text: "Clear"')


def test_approve_and_deny_take_their_colours_from_palette_roles():
    rules = {selector: declarations(body) for _, selector, body in css_rules(style())}
    assert rules[".link.approve"] == {"color": "var(--positive)"}
    assert rules[".link.deny"] == {"color": "var(--destructive)"}
