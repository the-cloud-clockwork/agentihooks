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
    assert exc.value.code == 1
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
