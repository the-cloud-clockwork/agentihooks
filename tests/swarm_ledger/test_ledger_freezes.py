import itertools

import pytest

from hooks.context import operator_words
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_freezes, ledger_tasks, new_ledger
from scripts.swarm_ledger.api import resources, schemas
from scripts.swarm_ledger.repository import rows
from tests.swarm_ledger import legacy_page

SLUG = "freeze-records"
IDS = itertools.count()
MASTER, ENGINEER, STRANGER = "master@f2e3d4-0001", "engineer@f2e3d4-0002", "master@f2e3d4-0009"
DISPATCHER, CI, PLANNER = "dispatcher@f2e3d4-0003", "ci@f2e3d4-0004", "planner@f2e3d4-0005"
WORDS = "Freeze the swarm v2 plan while we ship the hierarchy"


@pytest.fixture(autouse=True)
def freeze_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    for name in ledger_tasks.OPS:
        monkeypatch.setitem(core.EXTENSION_OPS, name, ledger_tasks)
    monkeypatch.setattr(ledger_freezes, "autonomy", lambda slug: "delegate")
    content = {
        "title": "Freeze",
        "overview": "Intent",
        "sources": [],
        "phases": [{"title": "Build"}, {"title": "Ship"}],
    }
    html, json_path = core.paths(SLUG)
    json_path.unlink(missing_ok=True)
    html.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765))
    core.sync(SLUG)
    for kind, fields in (
        ("plan_add", {"plan": "a", "title": "Plan a"}),
        ("plan_add", {"plan": "b", "title": "Plan b"}),
        ("phase_update", {"item": "phases/p1", "fields": {"plan": "plans/a"}}),
        ("phase_update", {"item": "phases/p2", "fields": {"plan": "plans/b"}}),
        ("slice_add", {"phase": "phases/p1", "anchor": "first"}),
        ("task_add", {"task": "t1", "title": "Build it", "lane": "eng", "phase": "p1", "slice": "slices/a.first"}),
        ("task_add", {"task": "t2", "title": "Ship it", "lane": "ci", "phase": "p2"}),
    ):
        op = {"op": kind, "id": f"{kind}-{next(IDS)}", "by": "planner", **fields}
        core.check_op(op)
        assert core.sync(SLUG, ops=[op])[1] == []
    core.sync(SLUG, ops=[{"op": "join", "id": "j1", "by": MASTER, "role": "orchestrator"}])
    core.sync(SLUG, ops=[{"op": "join", "id": "j2", "by": ENGINEER}])


def write(kind, by=None, **fields):
    op = {"op": kind, "id": f"{kind}-{next(IDS)}", **fields}
    if by is not None:
        op["by"] = by
    core.check_op(op)
    state, rejected = core.sync(SLUG, ops=[op])
    return state, rejected


def freeze(target, verb="freeze", by=None, **fields):
    return write("freeze_set", by=by, verb=verb, target=target, **fields)


def targets(state):
    return [(row["verb"], row["target"]) for row in state["freezes"]]


def event_of(state):
    event = state["_meta"]["events"][-1]
    return {key: value for key, value in event.items() if key not in ("rev", "at")}


def test_freezes_are_a_ledger_collection_with_verb_target_author_time_and_reason():
    assert "freezes" in rows.COLLECTIONS
    assert "freezes" in resources.COLLECTIONS
    state, rejected = freeze("plans/a", reason="Ship the hierarchy first")
    assert rejected == []
    row = state["freezes"][0]
    assert row == {
        "id": row["id"],
        "verb": "freeze",
        "target": "plans/a",
        "by": "operator",
        "at": row["at"],
        "reason": "Ship the hierarchy first",
    }
    assert row["at"] > 0
    assert event_of(state) == {
        "by": "operator",
        "kind": "frozen",
        "target": "plans/a",
        "id": row["id"],
        "reason": "Ship the hierarchy first",
    }
    assert state["_meta"]["stamps"]["freezes"]["by"] == "operator"
    state, _ = freeze("plans/b", verb="focus")
    focus = state["freezes"][1]
    assert focus == {
        "id": focus["id"],
        "verb": "focus",
        "target": "plans/b",
        "by": "operator",
        "at": focus["at"],
        "reason": "",
    }
    assert event_of(state) == {
        "by": "operator",
        "kind": "focused",
        "target": "plans/b",
        "id": focus["id"],
        "reason": "",
    }
    assert targets(state) == [("freeze", "plans/a"), ("focus", "plans/b")]


def test_an_operator_quote_is_kept_on_the_record():
    state, rejected = freeze("phases/p2", quote="hold ship")
    assert rejected == []
    assert state["freezes"][0]["quote"] == "hold ship"


def test_setting_the_same_verb_on_the_same_target_twice_keeps_one_record():
    freeze("phases/p1")
    state, rejected = freeze("phases/p1")
    assert rejected == []
    assert targets(state) == [("freeze", "phases/p1")]


def test_lane_and_kind_selectors_are_targets():
    freeze("lane:ci")
    state, rejected = freeze("kind:research", verb="focus")
    assert rejected == []
    assert targets(state) == [("freeze", "lane:ci"), ("focus", "kind:research")]


@pytest.mark.parametrize(
    "op, message",
    [
        ({"verb": "hold", "target": "plans/a"}, "freeze_set needs verb freeze or focus"),
        ({"verb": "freeze", "target": "plan a"}, "a freeze target is a plan, phase, slice or task address"),
        ({"verb": "freeze", "target": "lane:night"}, "a freeze target is a plan, phase, slice or task address"),
        ({"verb": "freeze", "target": "kind:chore"}, "a freeze target is a plan, phase, slice or task address"),
        ({"verb": "freeze", "target": "plans/a", "reason": 3}, "reason and quote must be text"),
        ({"verb": "freeze", "target": "plans/a", "extra": 1}, "freeze_set takes only verb target reason quote"),
        ({"verb": "freeze", "target": "lane:ci:x"}, "a freeze target is a plan, phase, slice or task address"),
        ({"verb": "freeze", "target": "plans/a", "by": "operator"}, "invalid freeze author"),
        ({"verb": "freeze", "target": "plans/a", "by": "1bad"}, "invalid freeze author"),
        ({"verb": "freeze", "target": "plans/a", "by": 7}, "invalid freeze author"),
        ({"verb": "freeze", "target": "plans/a", "quote": 3}, "reason and quote must be text"),
        ({"verb": "freeze", "target": "plans/a", "quote": "q" * 2001}, "reason and quote must be text"),
        ({"verb": "freeze", "target": "plans/a", "reason": "r" * 2001}, "reason and quote must be text"),
    ],
)
def test_a_malformed_freeze_is_refused_at_check(op, message):
    with pytest.raises(ValueError) as raised:
        core.check_op({"op": "freeze_set", "id": "f", **op})
    assert str(raised.value) == message


def test_reason_and_quote_may_reach_the_text_limit():
    text = "x" * ledger_freezes.MAX_TEXT
    core.check_op({"op": "freeze_set", "id": "f", "verb": "freeze", "target": "plans/a", "reason": text, "quote": text})


def test_a_freeze_on_a_node_the_ledger_lacks_is_rejected():
    state, rejected = freeze("phases/p9")
    assert rejected and state["freezes"] == []


def test_unfreezing_a_plan_clears_freezes_on_its_phases_slices_and_tasks():
    for target in ("plans/a", "phases/p1", "slices/a.first", "tasks/t1", "phases/p2", "tasks/t2", "lane:ci"):
        freeze(target)
    freeze("phases/p1", verb="focus")
    state, rejected = write("freeze_clear", target="plans/a", reason="hierarchy shipped")
    assert rejected == []
    assert targets(state) == [("freeze", "phases/p2"), ("freeze", "tasks/t2"), ("freeze", "lane:ci")]
    assert event_of(state) == {
        "by": "operator",
        "kind": "unfrozen",
        "target": "plans/a",
        "id": state["_meta"]["events"][-1]["id"],
        "cleared": ["plans/a", "phases/p1", "slices/a.first", "tasks/t1", "phases/p1"],
        "reason": "hierarchy shipped",
    }
    assert state["_meta"]["events"][-1]["id"].startswith("freeze_clear-")
    stamp = state["_meta"]["stamps"]["freezes"]
    assert (stamp["by"], stamp["rev"]) == ("operator", state["_meta"]["rev"])


def test_a_task_group_lead_holds_its_members_under_it():
    doc = {
        "phases": [{"id": "p1"}],
        "tasks": [
            {"id": "t1", "phase": "p1", "group_members": ["t3"]},
            {"id": "t2", "phase": "p1"},
            {"id": "t3", "phase": "p1", "merged_into": "t1"},
        ],
    }
    assert ledger_freezes.under(doc, "tasks/t1") == {"tasks/t1", "tasks/t3"}
    assert ledger_freezes.under(doc, "phases/p1") == {"phases/p1", "tasks/t1", "tasks/t2", "tasks/t3"}
    assert ledger_freezes.under(doc, "tasks/t2") == {"tasks/t2"}


def test_clearing_a_selector_removes_only_that_selector():
    freeze("lane:ci")
    freeze("kind:ci")
    state, rejected = write("freeze_clear", target="lane:ci")
    assert rejected == []
    assert targets(state) == [("freeze", "kind:ci")]
    assert event_of(state)["cleared"] == ["lane:ci"]
    assert event_of(state)["reason"] == ""


def test_clearing_a_target_without_records_changes_nothing():
    freeze("phases/p2")
    state, rejected = write("freeze_clear", target="phases/p1")
    assert rejected == []
    assert targets(state) == [("freeze", "phases/p2")]


def test_an_engineers_freeze_write_is_refused():
    state, rejected = freeze("plans/a", by=ENGINEER, quote=WORDS)
    assert rejected and state["freezes"] == []
    freeze("plans/a")
    state, rejected = write("freeze_clear", by=ENGINEER, target="plans/a")
    assert rejected and targets(state) == [("freeze", "plans/a")]


def test_the_master_writes_a_freeze_only_with_the_operators_recorded_words():
    state, rejected = freeze("plans/a", by=MASTER, quote="freeze the swarm v2 plan")
    assert rejected and state["freezes"] == []
    operator_words.record(MASTER, WORDS)
    state, rejected = freeze("plans/a", by=MASTER, quote="freeze the swarm v2 plan")
    assert rejected == []
    assert state["freezes"][0]["by"] == MASTER
    assert state["freezes"][0]["quote"] == "Freeze the swarm v2 plan"
    state, rejected = freeze("plans/b", by=MASTER)
    assert rejected and targets(state) == [("freeze", "plans/a")]
    state, rejected = write("freeze_clear", by=MASTER, target="plans/a", quote="freeze the swarm v2 plan")
    assert rejected == [] and state["freezes"] == []
    assert event_of(state)["by"] == MASTER
    assert state["_meta"]["stamps"]["freezes"]["by"] == MASTER


def test_an_author_who_never_joined_is_refused_without_error():
    operator_words.record(STRANGER, WORDS)
    state, rejected = freeze("plans/a", by=STRANGER, quote="freeze the swarm v2 plan")
    assert rejected and state["freezes"] == []


def test_the_dispatcher_writes_freezes_only_at_full_autonomy(monkeypatch):
    state, rejected = freeze("plans/a", by=ledger_freezes.DISPATCHER)
    assert rejected and state["freezes"] == []
    asked = []
    monkeypatch.setattr(ledger_freezes, "autonomy", lambda slug: asked.append(slug) or "full")
    state, rejected = freeze("plans/a", by=ledger_freezes.DISPATCHER)
    assert rejected == []
    row = state["freezes"][0]
    assert row == {
        "id": row["id"],
        "verb": "freeze",
        "target": "plans/a",
        "by": "dispatcher",
        "at": row["at"],
        "reason": "",
    }
    assert asked == [SLUG]


def test_the_live_dispatcher_at_full_autonomy_freezes_focuses_and_unfreezes_under_its_name(monkeypatch):
    monkeypatch.setattr(ledger_freezes, "autonomy", lambda slug: "full")
    assert freeze("plans/a", by=DISPATCHER)[1] == []
    state, rejected = freeze("plans/b", verb="focus", by=DISPATCHER, reason="ship b")
    assert rejected == []
    assert [(row["verb"], row["target"], row["by"], row["reason"]) for row in state["freezes"]] == [
        ("freeze", "plans/a", DISPATCHER, ""),
        ("focus", "plans/b", DISPATCHER, "ship b"),
    ]
    state, rejected = write("freeze_clear", by=DISPATCHER, target="plans/a")
    assert rejected == []
    assert [row["target"] for row in state["freezes"]] == ["plans/b"]
    assert event_of(state)["by"] == DISPATCHER
    assert state["_meta"]["stamps"]["freezes"]["by"] == DISPATCHER


@pytest.mark.parametrize("autonomy", ["", "manual", "assist", "delegate"])
@pytest.mark.parametrize("verb", ["freeze", "focus", "unfreeze"])
def test_the_live_dispatcher_below_full_autonomy_writes_no_freeze(monkeypatch, autonomy, verb):
    freeze("plans/a")
    monkeypatch.setattr(ledger_freezes, "autonomy", lambda slug: autonomy)
    if verb == "unfreeze":
        state, rejected = write("freeze_clear", by=DISPATCHER, target="plans/a")
    else:
        state, rejected = freeze("plans/b", verb=verb, by=DISPATCHER)
    assert rejected
    assert [(row["target"], row["by"]) for row in state["freezes"]] == [("plans/a", "operator")]


@pytest.mark.parametrize("name", [ENGINEER, CI, PLANNER, MASTER])
@pytest.mark.parametrize("verb", ["freeze", "focus", "unfreeze"])
def test_other_lanes_at_full_autonomy_write_no_freeze_without_the_operators_words(monkeypatch, name, verb):
    freeze("plans/a")
    monkeypatch.setattr(ledger_freezes, "autonomy", lambda slug: "full")
    if verb == "unfreeze":
        state, rejected = write("freeze_clear", by=name, target="plans/a")
    else:
        state, rejected = freeze("plans/b", verb=verb, by=name)
    assert rejected
    assert [(row["target"], row["by"]) for row in state["freezes"]] == [("plans/a", "operator")]


def test_lines_name_each_active_freeze_for_swarm_status():
    assert ledger_freezes.lines({}) == []
    freeze("plans/a", reason="Ship the hierarchy first")
    state, _ = freeze("lane:ci", verb="focus")
    for row, at in zip(state["freezes"], (0, 1791605100000)):
        row["at"] = at
    assert ledger_freezes.lines(state) == [
        "freeze  plans/a  by operator  at 1970-01-01T00:00Z  Ship the hierarchy first",
        "focus  lane:ci  by operator  at 2026-10-10T04:05Z",
    ]


def test_the_api_takes_freeze_ops_against_the_freezes_collection():
    ops = [
        {"op": "freeze_set", "id": "f1", "verb": "focus", "target": "plans/a", "reason": "r", "quote": "q"},
        {"op": "freeze_clear", "id": "f2", "by": MASTER, "target": "plans/a", "quote": "q"},
    ]
    assert schemas.check_operations({"operation_id": "o1", "ops": ops, "guards": {}}, core, ()) == ops
    assert [schemas.target(op) for op in ops] == ["freezes", "freezes"]
