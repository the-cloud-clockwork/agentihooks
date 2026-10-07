import json

import ledger
import ledger_core as core
import pytest

from tests.swarm_ledger.test_priorities import SLUG, add, make_ledger


def test_clear_by_item_removes_only_its_priority():
    state = make_ledger()
    item = f"followups/{state['followups'][0]['id']}"
    phase = f"phases/{state['phases'][0]['id']}"
    core.sync(SLUG, ops=[add(1, item, "Pick one."), add(2, phase, "Pick another.")])
    state, rejected = core.sync(SLUG, ops=[{"op": "priority_clear", "id": "clear", "target": item}])
    assert rejected == []
    assert [p["item"] for p in state["priorities"]] == [phase, f"questions/{state['questions'][0]['id']}"]


@pytest.mark.parametrize("targets, cleared", [(["followups/unknown"], []), (["pr-1", "followups/unknown"], ["pr-1"])])
def test_cli_rejected_clear_exits_nonzero_and_reports_only_successes(monkeypatch, capsys, targets, cleared):
    state = make_ledger()
    item = f"followups/{state['followups'][0]['id']}"
    core.sync(SLUG, ops=[add(1, item, "Pick one.")])

    def call(slug, ops):
        state, rejected = core.sync(slug, ops=ops)
        return {**state, "rejected": rejected}

    monkeypatch.setattr(ledger, "call", call)
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", "boss", "priority", "clear", *targets])
    with pytest.raises(SystemExit) as exc:
        ledger.cmd_priority(args)
    assert exc.value.code.startswith("rejected: ['priority_clear-")
    result = json.loads(capsys.readouterr().out)
    assert result["cleared"] == cleared
    assert len(result["rejected"]) == 1


@pytest.mark.parametrize("mode", ["id", "item", "all"])
def test_cli_clear_reports_accepted_targets(monkeypatch, capsys, mode):
    state = make_ledger()
    item = f"followups/{state['followups'][0]['id']}"
    core.sync(SLUG, ops=[add(1, item, "Pick one.")])

    def call(slug, ops):
        state, rejected = core.sync(slug, ops=ops)
        return {**state, "rejected": rejected}

    monkeypatch.setattr(ledger, "call", call)
    target = {"id": "pr-1", "item": item, "all": "all"}[mode]
    values = ["--all"] if mode == "all" else [target]
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", "boss", "priority", "clear", *values])
    ledger.cmd_priority(args)
    assert json.loads(capsys.readouterr().out) == {"cleared": [target], "rejected": []}
    state, _ = core.sync(SLUG)
    assert item not in [p["item"] for p in state["priorities"]]


def test_clear_records_its_reason_in_the_history():
    state = make_ledger()
    item = f"followups/{state['followups'][0]['id']}"
    core.sync(SLUG, ops=[add(1, item, "Pick one.")])
    clear = {"op": "priority_clear", "id": "c", "by": "swarm", "target": "pr-1", "reason": "its item is done"}
    state, rejected = core.sync(SLUG, ops=[clear])
    assert rejected == []
    event = [e for e in state["_meta"]["events"] if e["kind"] == "priority cleared"][-1]
    assert (event["by"], event["target"], event["reason"]) == ("swarm", item, "its item is done")


def test_clear_without_a_reason_records_none():
    state = make_ledger()
    item = f"followups/{state['followups'][0]['id']}"
    core.sync(SLUG, ops=[add(1, item, "Pick one.")])
    state, _ = core.sync(SLUG, ops=[{"op": "priority_clear", "id": "c", "target": "pr-1"}])
    assert "reason" not in [e for e in state["_meta"]["events"] if e["kind"] == "priority cleared"][-1]


@pytest.mark.parametrize("reason", [3, "", "  "])
def test_clear_refuses_a_reason_that_is_not_text(reason):
    from scripts.swarm_ledger import ledger_priorities

    with pytest.raises(ValueError, match="^priority_clear reason must be text$"):
        ledger_priorities.check({"op": "priority_clear", "id": "c", "target": "all", "reason": reason})


def test_clear_accepts_a_reason_in_words():
    from scripts.swarm_ledger import ledger_priorities

    assert ledger_priorities.check({"op": "priority_clear", "id": "c", "target": "all", "reason": "done"}) is None


@pytest.mark.parametrize(
    "extra, by, recorded",
    [({"by": "swarm", "reason": "its item is done"}, "swarm", "its item is done"), ({}, "operator", None)],
)
def test_clear_applies_with_or_without_a_reason(extra, by, recorded):
    from scripts.swarm_ledger import ledger_priorities

    row = {"id": "pr-1", "item": "followups/f1", "text": "Pick one.", "by": "boss", "at": 1}
    doc = {"phases": [], "priorities": [row], "followups": [{"id": "f1", "text": "x", "done": True}]}
    ctx = core.Context({"rev": 0, "stamps": {}, "events": []}, 5)
    op = {"op": "priority_clear", "id": "c", "target": "pr-1", **extra}
    assert ledger_priorities.apply(doc, op, ctx) is True
    assert doc["priorities"] == []
    assert ctx.events == [
        {
            "rev": 1,
            "at": 5,
            "by": by,
            "kind": "priority cleared",
            "target": "followups/f1",
            "id": "pr-1",
            "text": "Pick one.",
            **({"reason": recorded} if recorded else {}),
        }
    ]
