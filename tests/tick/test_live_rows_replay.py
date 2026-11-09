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
FEATURE = "swarm-tick-live-rows"


def _ledger() -> dict[str, Any]:
    """The 9 November ledger, its ids hashed, as the ledger server holds it."""
    return {**json.loads(gzip.decompress(FIXTURE.read_bytes())), "_meta": {"events": []}}


def _live_unfrozen(doc: dict[str, Any]) -> int:
    """Tasks that are not done and not an open task a freeze holds, counted by the claim gate's own freeze walk."""
    graph = hierarchy.project(doc)[0]
    return sum(
        task["state"] != "done"
        and not (task["state"] == "open" and freeze.frozen(task, doc["freezes"], freeze.ancestry(task, graph)))
        for task in doc["tasks"]
    )


def _reader(doc: dict[str, Any], paths: set[str] | None = None) -> view.Reader:
    """The export route's answer for the tick view's default read of doc: full, or the changes along paths."""

    def read(since: int, parts: tuple[str, ...]) -> dict[str, Any]:
        named = tuple(sorted(parts))
        answer = live_rows.answer(doc, live_rows.Scope())
        changes = None if paths is None or not since else routes.export_changes(doc, paths, named, answer)
        body = changes if changes is not None else routes.export_text(fragments.document(doc), named, answer)
        return {"generation": doc["generation"], **json.loads(body)}

    return read


def _pass(path: Path, reader: view.Reader) -> dict[str, Any]:
    """The pass record of one pass that reads the view at path through reader."""
    record: dict[str, Any] = {}

    def run() -> None:
        timing.PASS.set(record)
        view.refreshed(path, reader)

    copy_context().run(run)
    return record


@pytest.mark.feature(FEATURE)
def test_the_replayed_ledger_holds_far_more_tasks_than_a_pass_may_read() -> None:
    doc = _ledger()
    assert len(doc["tasks"]) == 4880
    assert len(doc["freezes"]) == 1332
    assert _live_unfrozen(doc) == 292


@pytest.mark.feature(FEATURE)
def test_a_pass_reads_only_the_live_unfrozen_tasks_of_the_replayed_ledger(tmp_path: Path) -> None:
    doc = _ledger()
    record = _pass(tmp_path / "view.json", _reader(doc))
    assert record[view.ROWS_READ] == _live_unfrozen(doc) == 292


@pytest.mark.feature(FEATURE)
def test_a_delta_pass_after_a_task_closes_reads_one_row_fewer(tmp_path: Path) -> None:
    doc = _ledger()
    path = tmp_path / "view.json"
    _pass(path, _reader(doc))
    after = copy.deepcopy(doc)
    closing = next(task for task in after["tasks"] if task["state"] == "claimed")
    closing["state"] = "done"
    after["generation"] = doc["generation"] + 1
    changed = {encode(["tasks", ["id", closing["id"], 0], "state"])}
    record = _pass(path, _reader(after, changed))
    assert record[view.ROWS_READ] == _live_unfrozen(after) == 291
    assert closing["id"] not in {task["id"] for task in view.refreshed(path, _reader(after, changed)).tasks}


@pytest.mark.feature(FEATURE)
def test_the_held_view_names_the_frozen_tasks_it_left_out(tmp_path: Path) -> None:
    doc = _ledger()
    held = view.refreshed(tmp_path / "view.json", _reader(doc))
    graph = hierarchy.project(doc)[0]
    frozen = {
        task["id"]
        for task in doc["tasks"]
        if task["state"] == "open" and freeze.frozen(task, doc["freezes"], freeze.ancestry(task, graph))
    }
    assert held.frozen_ids == frozen
    assert len(frozen) == 860
    assert not {task["id"] for task in held.tasks} & frozen
