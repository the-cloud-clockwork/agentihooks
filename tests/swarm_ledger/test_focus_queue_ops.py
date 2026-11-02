"""The focus op keeps one ordered queue: only replaces it, top and bottom place a target, focus and freeze exclude."""

import pytest

from scripts.swarm_ledger import ledger_core as core
from tests.swarm_ledger.test_ledger_freezes import freeze, freeze_ledger, targets, write  # noqa: F401

pytestmark = [pytest.mark.unit, pytest.mark.feature("swarm-focus")]


def test_focus_only_replaces_every_other_focus_and_keeps_the_freezes() -> None:
    freeze("lane:ci")
    freeze("plans/a", verb="focus")
    freeze("tasks/t2", verb="focus")
    state, rejected = freeze("phases/p2", verb="focus", only=True)
    assert rejected == []
    assert targets(state) == [("freeze", "lane:ci"), ("focus", "phases/p2")]


def test_focus_top_and_bottom_set_the_queue_order() -> None:
    freeze("plans/a", verb="focus")
    freeze("tasks/t2", verb="focus", position="top")
    freeze("lane:ci")
    state, _ = freeze("phases/p1", verb="focus", position="bottom")
    assert targets(state) == [
        ("focus", "tasks/t2"),
        ("focus", "plans/a"),
        ("freeze", "lane:ci"),
        ("focus", "phases/p1"),
    ]
    state, _ = freeze("phases/p1", verb="focus", position="top")
    assert targets(state)[0] == ("focus", "phases/p1")
    assert len(state["freezes"]) == 4
    assert state["_meta"]["events"][-1]["kind"] == "focused"


def test_focusing_a_frozen_task_unfreezes_it_and_a_focus_on_a_plan_unfreezes_everything_beneath() -> None:
    freeze("tasks/t1")
    freeze("phases/p2")
    state, _ = freeze("tasks/t1", verb="focus")
    assert targets(state) == [("freeze", "phases/p2"), ("focus", "tasks/t1")]
    for target in ("tasks/t1", "slices/a.first", "phases/p1", "plans/b", "lane:eng"):
        freeze(target)
    state, _ = freeze("plans/a", verb="focus")
    assert targets(state) == [
        ("freeze", "phases/p2"),
        ("focus", "tasks/t1"),
        ("freeze", "plans/b"),
        ("freeze", "lane:eng"),
        ("focus", "plans/a"),
    ]


def test_freezing_a_focused_target_ends_its_focus() -> None:
    freeze("tasks/t1", verb="focus")
    state, _ = freeze("tasks/t1")
    assert targets(state) == [("freeze", "tasks/t1")]


def test_unfreezing_a_task_leaves_it_free() -> None:
    freeze("tasks/t2", verb="focus")
    freeze("tasks/t1")
    state, _ = write("freeze_clear", target="tasks/t1")
    assert targets(state) == [("focus", "tasks/t2")]


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"verb": "freeze", "position": "top"}, "position and only apply to a focus"),
        ({"verb": "freeze", "only": True}, "position and only apply to a focus"),
        ({"verb": "focus", "position": "middle"}, "position is top or bottom and only is true or false"),
        ({"verb": "focus", "only": "yes"}, "position is top or bottom and only is true or false"),
    ],
)
def test_a_malformed_queue_field_is_refused_at_check(fields: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        core.check_op({"op": "freeze_set", "id": "f1", "target": "plans/a", **fields})


def test_focus_bottom_moves_a_queued_target_to_the_end_and_a_plain_refocus_keeps_its_place() -> None:
    freeze("tasks/t1", verb="focus")
    freeze("tasks/t2", verb="focus")
    state, _ = freeze("tasks/t1", verb="focus")
    assert targets(state) == [("focus", "tasks/t1"), ("focus", "tasks/t2")]
    state, _ = freeze("tasks/t1", verb="focus", position="bottom")
    assert targets(state) == [("focus", "tasks/t2"), ("focus", "tasks/t1")]


def test_the_queue_the_op_writes_is_the_claim_order() -> None:
    from scripts.swarm import focus_budget

    freeze("tasks/t1", verb="focus")
    state, _ = freeze("tasks/t2", verb="focus", position="top")
    assert focus_budget.ranks(state) == {"t1": 1, "t2": 0}


def test_an_unfrozen_task_waits_for_the_open_focus_before_it_may_be_claimed() -> None:
    from scripts.swarm import freeze as gate
    from scripts.swarm_ledger.repository import hierarchy

    freeze("tasks/t2", verb="focus")
    freeze("tasks/t1")
    state, _ = write("freeze_clear", target="tasks/t1")
    graph = hierarchy.project(state)[0]
    first = next(row for row in state["tasks"] if row["id"] == "t1")
    assert gate.claim_gate(first, state, graph, "")
    assert not gate.frozen(first, state["freezes"], gate.ancestry(first, graph))


def test_only_must_be_a_boolean() -> None:
    with pytest.raises(ValueError, match="position is top or bottom and only is true or false"):
        core.check_op({"op": "freeze_set", "id": "f1", "verb": "focus", "target": "plans/a", "only": 1})
