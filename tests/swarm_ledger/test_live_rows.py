"""The ledger read path a tick pass uses answers only live tasks, focused or free, with freeze and focus resolved."""

import copy
import json
from typing import Any

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


@pytest.mark.feature("swarm-tick-live-rows")
def test_a_scoped_full_export_names_frozen_ids_only_when_it_carries_the_tasks() -> None:
    doc = _doc()
    answer = live_rows.answer(doc, live_rows.Scope())
    whole = json.loads(routes.export_text(fragments.document(doc), (), answer))["data"]
    assert whole["frozen_ids"] == ["c"]
    assert whole["tasks"] == answer.rows
    phases = json.loads(routes.export_text(fragments.document(doc), ("phases",), answer))["data"]
    assert phases == {"phases": doc["phases"]}


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
    reply = json.loads(
        routes.export_changes(doc, {_path("phases", ["id", "p1", 0], "review")}, ("tasks",), answer) or "null"
    )
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
@pytest.mark.parametrize(
    ("payload", "scope"),
    [
        ({"since": 0}, None),
        ({"since": 0, "frozen": True}, live_rows.Scope(closed=False, frozen=True)),
        ({"since": 0, "closed": True}, live_rows.Scope(closed=True, frozen=False)),
        ({"since": 0, "closed": False, "frozen": False}, live_rows.Scope()),
    ],
)
def test_a_read_names_its_scope_only_through_the_flags_it_sends(
    payload: dict[str, object], scope: live_rows.Scope | None
) -> None:
    assert routes.scope_of(payload) == scope


@pytest.mark.feature("swarm-tick-live-rows")
def test_marks_cover_every_task_of_a_ledger_without_freezes() -> None:
    doc = {"tasks": [{"id": "a", "state": "open"}, {"id": "b", "state": "done"}, {"id": "c", "state": "open"}]}
    assert live_rows.marks(doc) == {"a": (False, None), "b": (False, None), "c": (False, None)}
    assert [row["id"] for row in live_rows.answer(doc, live_rows.Scope()).rows] == ["a", "c"]


@pytest.mark.feature("swarm-tick-live-rows")
def test_an_empty_or_unrelated_path_never_reshapes_the_rows() -> None:
    assert live_rows.reshapes([]) is False
    assert live_rows.reshapes(["followups", ["id", "x", 0], "text"]) is False
    assert live_rows.reshapes(["tasks", ["id", "a", 0], "state"]) is False


@pytest.mark.feature("swarm-tick-live-rows")
@pytest.mark.parametrize(
    ("target", "frozen"),
    [
        ("lane:eng", {"a"}),
        ("kind:code", {"a"}),
        ("kind:ops", {"o"}),
        ("lane:", set()),
        ("tasks/a", {"a"}),
        ("other:x", set()),
    ],
)
def test_a_selector_freeze_holds_the_tasks_its_lane_or_kind_names(target: str, frozen: set[str]) -> None:
    doc = {
        "tasks": [
            {"id": "a", "state": "open", "lane": "eng"},
            {"id": "o", "state": "open", "lane": "ci", "kind": "ops"},
        ],
        "freezes": [{"id": "f", "verb": "freeze", "target": target}],
    }
    assert {task_id for task_id, (cold, _) in live_rows.marks(doc).items() if cold} == frozen


@pytest.mark.feature("swarm-tick-live-rows")
def test_frozen_ids_ride_a_scoped_delta_only_when_it_names_the_tasks() -> None:
    doc = _doc()
    answer = live_rows.answer(doc, live_rows.Scope())
    paths = {_path("tasks", ["id", "a", 0], "state")}
    whole = json.loads(routes.export_changes(doc, paths, (), answer) or "null")
    assert [["frozen_ids"], ["c"]] in whole["changes"]["set"]
    phases = json.loads(routes.export_changes(doc, paths, ("phases",), answer) or "null")
    assert phases["changes"]["set"] == []


def _Entry(state: dict[str, Any]) -> Any:
    """A stored ledger entry at generation 7 holding state and no views."""
    from scripts.swarm_ledger.repository.entry import Entry

    return Entry(7, "", state, fragments.document(state))


def _repository(entry: Any, monkeypatch: pytest.MonkeyPatch, paths: set[str] | None) -> Any:
    """A repository answering entry for every ledger, with paths as the changes recorded since any generation."""
    from contextlib import contextmanager

    from scripts.swarm_ledger.repository import refresh, sqlite

    class _Connection:
        def execute(self, *_args: object) -> None:
            """Accept the transaction start."""

        def __enter__(self) -> "_Connection":
            """Enter the transaction."""
            return self

        def __exit__(self, *_args: object) -> None:
            """Leave the transaction."""

    @contextmanager
    def connect() -> Any:
        """Yield the stand in connection."""
        yield _Connection()

    repository = object.__new__(sqlite.SQLiteLedgerRepository)
    monkeypatch.setattr(repository, "_adopt", lambda slug: None, raising=False)
    monkeypatch.setattr(repository, "connect", connect, raising=False)
    monkeypatch.setattr(repository, "_entry", lambda connection, slug: entry, raising=False)
    monkeypatch.setattr(refresh, "changed_paths", lambda *args: paths)
    return repository


@pytest.mark.feature("swarm-tick-live-rows")
def test_each_scope_keeps_its_own_answer_for_a_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _Entry(_doc())
    repository = _repository(entry, monkeypatch, None)
    default = repository._answer(entry, live_rows.Scope())
    frozen = repository._answer(entry, live_rows.Scope(frozen=True))
    assert default == live_rows.answer(entry.state, live_rows.Scope())
    assert frozen == live_rows.answer(entry.state, live_rows.Scope(frozen=True))
    assert default.frozen_ids == ["c"] and frozen.frozen_ids == []
    assert repository._answer(entry, live_rows.Scope()) is default


@pytest.mark.feature("swarm-tick-live-rows")
def test_a_scoped_delta_hands_the_scope_answer_to_the_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _Entry(_doc())
    repository = _repository(entry, monkeypatch, {_path("tasks", ["id", "a", 0], "state")})
    seen: list[tuple[object, ...]] = []

    def changes(*args: object) -> str:
        """Record what the export hands the changes builder."""
        seen.append(args)
        return "{}"

    generation, text = repository.export_since("s", 3, routes.export_text, changes, ("tasks",), live_rows.Scope())
    assert (generation, text) == (7, "{}")
    assert seen[0][3] == live_rows.answer(entry.state, live_rows.Scope())
    repository.export_since("s", 3, routes.export_text, changes, ("tasks",))
    assert len(seen[1]) == 3


@pytest.mark.feature("swarm-tick-live-rows")
def test_a_scoped_full_read_builds_the_export_from_the_scope_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _Entry(_doc())
    repository = _repository(entry, monkeypatch, None)
    _, text = repository.export_since("s", 0, routes.export_text, routes.export_changes, ("tasks",), live_rows.Scope())
    assert json.loads(text)["data"]["frozen_ids"] == ["c"]
    _, whole = repository.export_since("s", 0, routes.export_text, routes.export_changes, ("tasks",))
    assert [task["id"] for task in json.loads(whole)["data"]["tasks"]] == ["a", "b", "c", "d", "e", "f"]


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
