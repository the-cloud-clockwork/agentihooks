"""The ledger read path a tick pass uses answers only live tasks, focused or free, with freeze and focus resolved."""

import copy
import json

import pytest

from scripts.swarm import freeze
from scripts.swarm_ledger.api import routes
from scripts.swarm_ledger.api.client import patched
from scripts.swarm_ledger.repository import fragments, hierarchy, live_rows
from scripts.swarm_ledger.repository.rows import encode


def _doc() -> dict:
    """A ledger with a done task, a frozen phase and a focus inside it."""
    return {
        "_meta": {"events": []},
        "phases": [{"id": "p1"}, {"id": "p2"}],
        "tasks": [
            {"id": "a", "state": "open", "phase": "p1", "lane": "eng"},
            {"id": "b", "state": "done", "phase": "p1", "lane": "eng"},
            {"id": "c", "state": "open", "phase": "p2", "lane": "eng"},
            {"id": "d", "state": "claimed", "phase": "p2", "lane": "eng"},
            {"id": "e", "state": "open", "phase": "p2", "lane": "ci"},
            {"id": "f", "state": "open", "phase": "p1", "lane": "ci"},
        ],
        "freezes": [
            {"id": "f1", "verb": "freeze", "target": "phases/p2"},
            {"id": "f2", "verb": "focus", "target": "tasks/e"},
            {"id": "f3", "verb": "focus", "target": "lane:ci"},
        ],
    }


def _ids(rows: list[dict]) -> list[str]:
    """The ids of rows in order."""
    return [row["id"] for row in rows]


@pytest.mark.feature("swarm-tick-live-rows")
def test_marks_resolve_freeze_and_focus_per_task() -> None:
    marks = live_rows.marks(_doc())
    assert marks == {
        "a": (False, None),
        "b": (False, None),
        "c": (True, None),
        "d": (True, None),
        "e": (False, 0),
        "f": (False, 2),
    }


@pytest.mark.feature("swarm-tick-live-rows")
def test_marks_agree_with_the_claim_gate_freeze() -> None:
    doc = _doc()
    graph = hierarchy.project(doc)[0]
    marks = live_rows.marks(doc)
    for task in doc["tasks"]:
        expected = freeze.frozen(task, doc["freezes"], freeze.ancestry(task, graph))
        assert marks[task["id"]][0] is expected, task["id"]


@pytest.mark.feature("swarm-tick-live-rows")
def test_the_claim_gate_reads_the_resolved_flag_as_its_own_walk_in_every_state() -> None:
    doc = _doc()
    graph = hierarchy.project(doc)[0]
    for row in live_rows.answer(doc, live_rows.Scope(closed=True, frozen=True)).rows:
        bare = {key: value for key, value in row.items() if key not in ("frozen", "focus_rank")}
        walked = freeze.frozen(bare, doc["freezes"], freeze.ancestry(bare, graph))
        assert freeze.frozen(row, [], []) is walked, row["id"]


@pytest.mark.feature("swarm-tick-live-rows")
def test_a_claimed_task_under_a_freeze_stays_in_the_default_rows() -> None:
    rows = {row["id"]: row for row in live_rows.answer(_doc(), live_rows.Scope()).rows}
    assert rows["d"]["state"] == "claimed"
    assert rows["d"]["frozen"] is True


@pytest.mark.feature("swarm-tick-live-rows")
def test_a_blocked_task_under_a_freeze_leaves_the_default_rows() -> None:
    doc = _doc()
    doc["tasks"].append({"id": "g", "state": "blocked", "phase": "p2", "lane": "eng"})
    answer = live_rows.answer(doc, live_rows.Scope())
    assert "g" not in {row["id"] for row in answer.rows}
    assert answer.frozen_ids == ["c", "g"]
    assert "g" in {row["id"] for row in live_rows.answer(doc, live_rows.Scope(frozen=True)).rows}


@pytest.mark.feature("swarm-tick-live-rows")
@pytest.mark.parametrize(
    ("closed", "frozen", "ids"),
    [
        (False, False, ["a", "d", "e", "f"]),
        (True, False, ["a", "b", "d", "e", "f"]),
        (False, True, ["a", "c", "d", "e", "f"]),
        (True, True, ["a", "b", "c", "d", "e", "f"]),
    ],
)
def test_rows_answer_closed_and_frozen_tasks_only_on_request(closed: bool, frozen: bool, ids: list[str]) -> None:
    rows = live_rows.answer(_doc(), live_rows.Scope(closed, frozen)).rows
    assert _ids(rows) == ids
    assert {row["id"]: (row["frozen"], row["focus_rank"]) for row in rows}.items() <= {
        "a": (False, None),
        "b": (False, None),
        "c": (True, None),
        "d": (True, None),
        "e": (False, 0),
        "f": (False, 2),
    }.items()


@pytest.mark.feature("swarm-tick-live-rows")
def test_the_default_scope_holds_neither_closed_nor_frozen_tasks() -> None:
    assert live_rows.Scope() == live_rows.Scope(closed=False, frozen=False)
    assert live_rows.answer(_doc(), live_rows.Scope()).frozen_ids == ["c"]
    assert live_rows.answer(_doc(), live_rows.Scope(frozen=True)).frozen_ids == []


@pytest.mark.feature("swarm-tick-live-rows")
def test_the_scoped_full_export_carries_only_the_scoped_task_rows() -> None:
    doc = _doc()
    answer = live_rows.answer(doc, live_rows.Scope())
    text = routes.export_text(fragments.document(doc), ("phases", "tasks"), answer)
    assert json.loads(text) == {"data": {"tasks": answer.rows, "phases": doc["phases"], "frozen_ids": ["c"]}}


@pytest.mark.feature("swarm-tick-live-rows")
def test_the_unscoped_full_export_keeps_every_task() -> None:
    doc = _doc()
    assert json.loads(routes.export_text(fragments.document(doc), ("tasks",)))["data"]["tasks"] == doc["tasks"]


def _path(*parts: object) -> str:
    """The stored row path of parts."""
    return encode(list(parts))


@pytest.mark.feature("swarm-tick-live-rows")
def test_scoped_changes_patch_the_held_rows_to_the_new_scoped_rows() -> None:
    before = _doc()
    held = {"tasks": live_rows.answer(before, live_rows.Scope()).rows}
    after = copy.deepcopy(before)
    after["tasks"][0]["state"] = "done"
    after["tasks"][3]["state"] = "pr"
    answer = live_rows.answer(after, live_rows.Scope())
    paths = {_path("tasks", ["id", "a", 0], "state"), _path("tasks", ["id", "d", 0], "state")}
    reply = json.loads(routes.export_changes(after, paths, ("tasks",), answer) or "null")
    assert reply is not None
    result = patched(held, reply["changes"])
    assert _ids(result["tasks"]) == ["d", "e", "f"]
    assert result == {"tasks": answer.rows, "frozen_ids": ["c"]}


@pytest.mark.feature("swarm-tick-live-rows")
@pytest.mark.parametrize(
    ("part", "field"),
    [
        ("freezes", "target"),
        ("phases", "plan"),
        ("phases", None),
        ("slices", "phase"),
        ("tasks", "merged_into"),
        ("tasks", "phase"),
    ],
)
def test_a_freeze_or_hierarchy_change_answers_the_scoped_rows_in_full(part: str, field: str | None) -> None:
    doc = {**_doc(), "slices": [{"id": "s1", "phase": "phases/p1"}]}
    answer = live_rows.answer(doc, live_rows.Scope())
    item = ["id", doc[part][0]["id"], 0]
    paths = {_path(part, item) if field is None else _path(part, item, field)}
    assert routes.export_changes(doc, paths, ("tasks",), answer) is None


@pytest.mark.feature("swarm-tick-live-rows")
def test_a_phase_change_outside_its_plan_link_stays_a_delta() -> None:
    doc = _doc()
    answer = live_rows.answer(doc, live_rows.Scope())
    reply = json.loads(routes.export_changes(doc, {_path("phases", ["id", "p1", 0], "review")}, ("tasks",), answer))
    assert reply["changes"]["set"] == [[["frozen_ids"], ["c"]]]


@pytest.mark.feature("swarm-tick-live-rows")
def test_unscoped_changes_still_carry_closed_task_rows() -> None:
    doc = _doc()
    reply = json.loads(routes.export_changes(doc, {_path("tasks", ["id", "b", 0], "state")}, ("tasks",)) or "null")
    touched = reply["changes"]["lists"]["tasks"]
    assert touched["items"] == {encode(["id", "b", 0]): doc["tasks"][1]}
    assert len(touched["order"]) == len(doc["tasks"])


@pytest.mark.feature("swarm-tick-live-rows")
def test_the_export_schema_takes_boolean_closed_and_frozen_flags() -> None:
    from scripts.swarm_ledger.api import schemas

    schemas.validate(routes.SINCE_SCHEMA, {"since": 0, "parts": ["tasks"], "closed": False, "frozen": True})
    for bad in ({"since": 0, "closed": "yes"}, {"since": 0, "frozen": 1}):
        with pytest.raises(Exception):
            schemas.validate(routes.SINCE_SCHEMA, bad)


@pytest.mark.feature("swarm-tick-live-rows")
def test_focus_holds_back_work_waiting_on_a_frozen_task_the_view_left_out() -> None:
    doc = {
        "phases": [],
        "tasks": [{"id": "x", "state": "open", "lane": "eng", "depends_on": ["y"]}],
        "freezes": [
            {"id": "f1", "verb": "focus", "target": "tasks/x"},
            {"id": "f2", "verb": "freeze", "target": "tasks/y"},
        ],
    }
    graph = hierarchy.project(doc)[0]
    assert freeze.admitted(doc, graph) == frozenset({"x", "y"})
    assert freeze.admitted({**doc, "frozen_ids": ["y"]}, graph) is None
