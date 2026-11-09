"""The ledger read path a tick pass uses answers only live tasks, focused or free, with freeze and focus resolved."""

import copy
import json

import pytest

from scripts.swarm import freeze
from scripts.swarm_ledger.api import routes
from scripts.swarm_ledger.api.client import patched
from scripts.swarm_ledger.repository import fragments, hierarchy, live_rows
from scripts.swarm_ledger.repository.rows import encode

FEATURE = "swarm-tick-live-rows"


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


@pytest.mark.feature(FEATURE)
def test_marks_resolve_freeze_and_focus_per_task() -> None:
    marks = live_rows.marks(_doc())
    assert marks == {
        "a": (False, None),
        "b": (False, None),
        "c": (True, None),
        "d": (False, None),
        "e": (False, 0),
        "f": (False, 2),
    }


@pytest.mark.feature(FEATURE)
def test_marks_agree_with_the_claim_gate_freeze() -> None:
    doc = _doc()
    graph = hierarchy.project(doc)[0]
    marks = live_rows.marks(doc)
    for task in doc["tasks"]:
        expected = task["state"] == "open" and freeze.frozen(task, doc["freezes"], freeze.ancestry(task, graph))
        assert marks[task["id"]][0] is expected, task["id"]


@pytest.mark.feature(FEATURE)
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
    rows = live_rows.rows(_doc(), live_rows.Scope(closed, frozen))
    assert _ids(rows) == ids
    assert {row["id"]: (row["frozen"], row["focus_rank"]) for row in rows}.items() <= {
        "a": (False, None),
        "b": (False, None),
        "c": (True, None),
        "d": (False, None),
        "e": (False, 0),
        "f": (False, 2),
    }.items()


@pytest.mark.feature(FEATURE)
def test_the_default_scope_holds_neither_closed_nor_frozen_tasks() -> None:
    assert live_rows.Scope() == live_rows.Scope(closed=False, frozen=False)
    assert live_rows.answer(_doc(), live_rows.Scope()).frozen_ids == ["c"]
    assert live_rows.answer(_doc(), live_rows.Scope(frozen=True)).frozen_ids == []


@pytest.mark.feature(FEATURE)
def test_the_scoped_full_export_carries_only_the_scoped_task_rows() -> None:
    doc = _doc()
    answer = live_rows.answer(doc, live_rows.Scope())
    text = routes.export_text(fragments.document(doc), ("phases", "tasks"), answer)
    assert json.loads(text) == {"data": {"tasks": answer.rows, "phases": doc["phases"], "frozen_ids": ["c"]}}


@pytest.mark.feature(FEATURE)
def test_the_unscoped_full_export_keeps_every_task() -> None:
    doc = _doc()
    assert json.loads(routes.export_text(fragments.document(doc), ("tasks",)))["data"]["tasks"] == doc["tasks"]


def _path(*parts: object) -> str:
    """The stored row path of parts."""
    return encode(list(parts))


@pytest.mark.feature(FEATURE)
def test_scoped_changes_patch_the_held_rows_to_the_new_scoped_rows() -> None:
    before = _doc()
    held = {"tasks": live_rows.rows(before, live_rows.Scope())}
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


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("part", ["freezes", "phases"])
def test_a_freeze_or_hierarchy_change_answers_the_scoped_rows_in_full(part: str) -> None:
    doc = _doc()
    answer = live_rows.answer(doc, live_rows.Scope())
    paths = {_path(part, ["id", doc[part][0]["id"], 0], "target")}
    assert routes.export_changes(doc, paths, ("tasks",), answer) is None


@pytest.mark.feature(FEATURE)
def test_unscoped_changes_still_carry_closed_task_rows() -> None:
    doc = _doc()
    reply = json.loads(routes.export_changes(doc, {_path("tasks", ["id", "b", 0], "state")}, ("tasks",)) or "null")
    touched = reply["changes"]["lists"]["tasks"]
    assert touched["items"] == {encode(["id", "b", 0]): doc["tasks"][1]}
    assert len(touched["order"]) == len(doc["tasks"])


@pytest.mark.feature(FEATURE)
def test_the_export_schema_takes_boolean_closed_and_frozen_flags() -> None:
    from scripts.swarm_ledger.api import schemas

    schemas.validate(routes.SINCE_SCHEMA, {"since": 0, "parts": ["tasks"], "closed": False, "frozen": True})
    for bad in ({"since": 0, "closed": "yes"}, {"since": 0, "frozen": 1}):
        with pytest.raises(Exception):
            schemas.validate(routes.SINCE_SCHEMA, bad)
