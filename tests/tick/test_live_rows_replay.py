"""Replay of the 9 November ledger: a tick pass reads at most the live unfrozen tasks, never the whole ledger."""

import copy
import gzip
import json
from contextvars import copy_context
from pathlib import Path
from typing import Any

import pytest

from scripts.swarm import freeze, timing
from scripts.swarm_ledger.api import routes
from scripts.swarm_ledger.repository import fragments, hierarchy, live_rows
from scripts.swarm_ledger.repository.rows import encode
from scripts.tick import view

FIXTURE = Path(__file__).parents[1] / "swarm_ledger" / "fixtures" / "ledger-2026-11-09.json.gz"


def _ledger() -> dict[str, Any]:
    """The 9 November ledger, its ids hashed, as the ledger server holds it."""
    return {**json.loads(gzip.decompress(FIXTURE.read_bytes())), "_meta": {"events": []}}


def _live_unfrozen(doc: dict[str, Any]) -> int:
    """Tasks that are not done and not an open or blocked task a freeze holds, counted by the claim gate's own freeze walk."""
    graph = hierarchy.project(doc)[0]
    return sum(
        task["state"] != "done"
        and not (
            task["state"] in ("open", "blocked") and freeze.frozen(task, doc["freezes"], freeze.ancestry(task, graph))
        )
        for task in doc["tasks"]
    )


def _reader(
    doc: dict[str, Any], paths: set[str] | None = None, scope: live_rows.Scope = live_rows.Scope()
) -> view.Reader:
    """The export route's answer for a scope read of doc: full, or the changes along paths."""

    def read(since: int, parts: tuple[str, ...], after: int | None = None) -> dict[str, Any]:
        named = tuple(sorted(parts))
        answer = live_rows.answer(doc, scope)
        changes = None if paths is None or not since else routes.export_changes(doc, paths, named, answer, after)
        body = changes if changes is not None else routes.export_text(fragments.document(doc), named, answer)
        return {"generation": doc["generation"], **json.loads(body)}

    return read


def _pass(path: Path, reader: view.Reader) -> tuple[dict[str, Any], view.LedgerView]:
    """The pass record of one pass that reads the view at path through reader, and the view it read."""
    record: dict[str, Any] = {}
    held: list[view.LedgerView] = []

    def run() -> None:
        timing.PASS.set(record)
        held.append(view.refreshed(path, reader))

    copy_context().run(run)
    return record, held[0]


@pytest.mark.feature("swarm-tick-live-rows")
def test_the_replayed_ledger_holds_far_more_tasks_than_a_pass_may_read() -> None:
    doc = _ledger()
    assert len(doc["tasks"]) == 4880
    assert len(doc["freezes"]) == 1332
    assert _live_unfrozen(doc) == 217


@pytest.mark.feature("swarm-tick-live-rows")
def test_a_default_scope_read_holds_only_the_live_unfrozen_tasks_of_the_replayed_ledger(tmp_path: Path) -> None:
    doc = _ledger()
    record, _ = _pass(tmp_path / "view.json", _reader(doc))
    assert record[view.ROWS_READ] == 217


@pytest.mark.feature("swarm-tick-live-rows")
def test_a_delta_pass_after_a_task_closes_reads_one_row_fewer(tmp_path: Path) -> None:
    doc = _ledger()
    path = tmp_path / "view.json"
    _, first = _pass(path, _reader(doc))
    after = copy.deepcopy(doc)
    kept = {row["id"] for row in first.tasks}
    closing = next(task for task in after["tasks"] if task["id"] in kept)
    closing["state"] = "done"
    after["generation"] = doc["generation"] + 1
    changed = {encode(["tasks", ["id", closing["id"], 0], "state"])}
    record, held = _pass(path, _reader(after, changed))
    assert record[view.ROWS_READ] == 216
    assert closing["id"] not in {task["id"] for task in held.tasks}


@pytest.mark.feature("swarm-tick-live-rows")
def test_the_held_view_names_the_frozen_tasks_it_left_out(tmp_path: Path) -> None:
    doc = _ledger()
    held = view.refreshed(tmp_path / "view.json", _reader(doc))
    graph = hierarchy.project(doc)[0]
    frozen = {
        task["id"]
        for task in doc["tasks"]
        if task["state"] in ("open", "blocked") and freeze.frozen(task, doc["freezes"], freeze.ancestry(task, graph))
    }
    assert held.frozen_ids == frozen
    assert len(frozen) == 935
    assert not {task["id"] for task in held.tasks} & frozen


@pytest.mark.feature("swarm-tick-live-rows")
def test_the_tick_scope_pass_still_reads_every_task_until_its_readers_move(tmp_path: Path) -> None:
    doc = _ledger()
    record, _ = _pass(tmp_path / "view.json", _reader(doc, scope=view.TICK_SCOPE))
    assert record[view.ROWS_READ] == len(doc["tasks"]) == 4880


@pytest.mark.feature("swarm-tick-live-rows")
def test_the_resolved_flag_matches_the_freeze_walk_for_every_replayed_task() -> None:
    doc = _ledger()
    graph = hierarchy.project(doc)[0]
    marks = live_rows.marks(doc)
    assert [
        task["id"]
        for task in doc["tasks"]
        if marks[task["id"]][0] is not freeze.frozen(task, doc["freezes"], freeze.ancestry(task, graph))
    ] == []


@pytest.mark.feature("swarm-tick-live-rows")
def test_a_pass_that_reads_an_empty_view_counts_no_rows() -> None:
    record: dict[str, Any] = {}

    def run() -> None:
        timing.PASS.set(record)
        view.counted(view.LedgerView(1, {"tasks": []}))

    copy_context().run(run)
    assert record == {view.ROWS_READ: 0}


@pytest.mark.feature("swarm-tick-live-rows")
def test_a_pass_keeps_the_most_rows_any_of_its_reads_held() -> None:
    record: dict[str, Any] = {}
    rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]

    def run() -> None:
        timing.PASS.set(record)
        view.counted(view.LedgerView(1, {"tasks": rows}))
        view.counted(view.LedgerView(2, {"tasks": rows[:1]}))

    copy_context().run(run)
    assert record == {view.ROWS_READ: 3}


@pytest.mark.feature("swarm-tick-live-rows")
def test_pass_state_filters_a_stand_in_read_only_for_a_scope_narrower_than_the_tick() -> None:
    from types import SimpleNamespace

    from scripts.swarm.ledger_client import pass_state

    doc = {
        "tasks": [{"id": "a", "state": "open"}, {"id": "b", "state": "done"}],
        "freezes": [],
    }
    stand_in = SimpleNamespace(state=lambda slug: doc)
    with view.holding():
        assert pass_state(stand_in, "sw") is doc
        assert pass_state(stand_in, "sw", view.TICK_SCOPE) is doc
        live = pass_state(stand_in, "sw", live_rows.Scope())
    assert [task["id"] for task in live["tasks"]] == ["a"]
    assert live[live_rows.FROZEN_IDS] == []


@pytest.mark.feature("swarm-tick-live-rows")
def test_the_pass_view_reads_the_ledger_with_the_tick_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from scripts.scheduler import service
    from scripts.swarm import ledger_client
    from scripts.swarm_ledger.api import client as api

    asked: list[object] = []

    def changes(
        self: api.ResourceClient,
        slug: str,
        since: int,
        parts: tuple[str, ...],
        scope: object = None,
        after: int | None = None,
    ) -> dict[str, Any]:
        """Answer a full read of an empty ledger and record the scope asked for."""
        asked.append(scope)
        return {"generation": 3, "data": {"tasks": []}}

    stand_in = SimpleNamespace(
        base=lambda slug: "http://ledger", credentials=lambda slug, service: {}, REQUEST_TIMEOUT=4
    )
    monkeypatch.setattr(ledger_client, "_ledger", lambda: stand_in)
    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(api.ResourceClient, "changes", changes)
    with view.holding():
        assert view.start("sw", False) is not None
    assert asked == [view.TICK_SCOPE]
